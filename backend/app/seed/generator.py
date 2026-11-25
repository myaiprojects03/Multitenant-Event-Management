"""Deterministic synthetic data generator. Pure: returns {table_name: [row dicts]}.

Planted patterns (per main event):
  * one talk where ~55% of the room leaves between minutes 20 and 26 (drop-off)
  * one upcoming workshop with far more bookmarks than seats (capacity risk)
  * one exhibitor with a lead spike yesterday 13:00-15:00 (lead velocity)
  * a mix of networking / exhibitor-matching opt-ins
The main events run anchor-2 .. anchor+1: two past days of history (the lead spike is
"yesterday"), a short program today, and tomorrow's program, which holds the overbooked
workshop so the capacity-risk demo works at any time of day after seeding.
"""

import random
import re
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from faker import Faker

from app.seed.catalog import (
    JOB_TITLES,
    KEYNOTE_TITLE,
    LOOKING_FOR,
    PANEL_PATTERN,
    PERSONA_TYPES,
    PERSONAS,
    SENIORITY,
    TENANTS,
    TITLE_PATTERNS,
    WORKSHOP_PATTERN,
    EventSpec,
    Persona,
    TenantSpec,
)

Rows = dict[str, list[dict]]

DURATION_MIN = {"keynote": 60, "talk": 45, "panel": 60, "workshop": 80}
OFFERING = (
    "domain expertise",
    "pilot site",
    "engineering talent",
    "funding",
    "integration experience",
    "customer introductions",
    "research data",
    "mentoring",
)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _tier(score: int) -> str:
    return "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 45 else "D"


class Generator:
    def __init__(self, anchor: date, seed: int = 42) -> None:
        self.anchor = anchor
        # "now" = the real time when seeding for today (sessions that already ended today get
        # check-ins), noon for an explicit past/future anchor. Tomorrow is always upcoming.
        real_now = datetime.now(UTC)
        self.now = (
            real_now if anchor == real_now.date() else datetime.combine(anchor, time(12), UTC)
        )
        self.rng = random.Random(seed)
        self.fake = Faker("en_US")
        self.fake.seed_instance(seed)
        self.rows: Rows = defaultdict(list)
        self._email_n = 0

    # --- helpers ------------------------------------------------------------------------------

    def uid(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def at(self, day_offset: int, hhmm: str, plus_min: float = 0) -> datetime:
        h, m = map(int, hhmm.split(":"))
        d = self.anchor + timedelta(days=day_offset)
        return datetime.combine(d, time(h, m), UTC) + timedelta(minutes=plus_min)

    def weighted_sample(self, items: list, weights: list[float], k: int) -> list:
        items, weights = list(items), list(weights)
        out = []
        for _ in range(min(k, len(items))):
            i = self.rng.choices(range(len(items)), weights)[0]
            out.append(items.pop(i))
            weights.pop(i)
        return out

    def add(self, table: str, **row) -> dict:
        self.rows[table].append(row)
        return row

    def new_user(self, tid: uuid.UUID, domain: str, persona: Persona | None = None) -> dict:
        if persona:
            return self.add(
                "users",
                id=self.uid(),
                tenant_id=tid,
                email=persona.email,
                full_name=persona.full_name,
                phone=self.fake.numerify("+1-555-###-####"),
                is_demo_persona=True,
                persona_label=persona.label,
            )
        first, last = self.fake.first_name(), self.fake.last_name()
        self._email_n += 1
        return self.add(
            "users",
            id=self.uid(),
            tenant_id=tid,
            email=f"{_slug(first)}.{_slug(last)}.{self._email_n}@{domain}",
            full_name=f"{first} {last}",
            phone=self.fake.numerify("+1-555-###-####"),
            is_demo_persona=False,
            persona_label=None,
        )

    # --- top level ----------------------------------------------------------------------------

    def build(self) -> Rows:
        for t in TENANTS:
            self.build_tenant(t)
        return dict(self.rows)

    def build_tenant(self, t: TenantSpec) -> None:
        tid = self.uid()
        self.add("tenants", id=tid, name=t.name, slug=t.slug)
        personas = PERSONAS[t.key]
        persona_users = {p.key: self.new_user(tid, t.email_domain, p) for p in personas}
        organizers = [persona_users[p.key] for p in personas if p.role == "organizer"]
        organizers += [self.new_user(tid, t.email_domain) for _ in range(2)]
        attendee_pool: list[dict] = []  # tenant-level, so some people attend several events
        for ev in t.events:
            EventBuilder(
                self, t, tid, ev, personas, persona_users, organizers, attendee_pool
            ).build()


class EventBuilder:
    def __init__(self, g, t, tid, ev: EventSpec, personas, persona_users, organizers, pool):
        self.g, self.t, self.tid, self.ev = g, t, tid, ev
        self.rng, self.fake = g.rng, g.fake
        self.personas = [p for p in personas if ev.key in p.events]
        self.persona_users = persona_users
        self.organizers = organizers
        self.pool = pool
        self.eid = g.uid()
        self.interests = sorted({tag for tags in ev.tracks.values() for tag in tags})
        self.past_days = [off for off, _ in ev.days if off < 0]

    def add(self, table: str, **row) -> dict:
        return self.g.add(table, tenant_id=self.tid, event_id=self.eid, **row)

    def build(self) -> None:
        ev, g = self.ev, self.g
        offsets = [off for off, _ in ev.days]
        g.add(
            "events",
            id=self.eid,
            tenant_id=self.tid,
            name=ev.name,
            slug=ev.slug,
            kind=ev.kind,
            venue=ev.venue,
            timezone="UTC",
            starts_on=g.anchor + timedelta(days=min(offsets)),
            ends_on=g.anchor + timedelta(days=max(offsets)),
        )
        self.build_exhibitors()
        self.build_people()
        self.build_program()
        self.build_sponsors()
        self.build_content()
        self.build_bookmarks()
        self.build_attendance()
        self.build_booths_and_leads()
        self.build_networking()
        self.build_app_activity()
        self.build_policies()

    # --- exhibitors -----------------------------------------------------------------------------

    def build_exhibitors(self) -> None:
        ev, rng = self.ev, self.rng
        cats = list(ev.exhibitor_categories)
        names = list(ev.fixed_exhibitors)
        others = [n for n in ev.exhibitor_names if n not in names]
        names += rng.sample(others, ev.n_exhibitors - len(names))
        booths = iter(f"{row}{n:02d}" for row in "ABCDEF" for n in range(1, 30))
        used_booths = {b for _, b in ev.fixed_exhibitors.values()}
        self.exhibitors: dict[str, dict] = {}
        for name in names:
            if name in ev.fixed_exhibitors:
                cat, booth = ev.fixed_exhibitors[name]
            else:
                cat = rng.choice(cats)
                booth = next(b for b in booths if b not in used_booths)
                used_booths.add(booth)
            tags = list(ev.exhibitor_categories[cat])
            extra = rng.choice(self.interests)
            if extra not in tags:
                tags.append(extra)
            x = self.add(
                "exhibitors",
                id=self.g.uid(),
                name=name,
                booth=booth,
                category=cat,
                description=(
                    f"{name} provides {cat.lower()} solutions focused on {tags[0]} and "
                    f"{tags[1] if len(tags) > 1 else tags[0]}. Visit booth {booth} for a live demo."
                ),
                tags=tags,
                website=f"https://{_slug(name)}.example.com",
            )
            self.exhibitors[name] = x
            self.add(
                "exhibitor_lead_criteria",
                id=self.g.uid(),
                exhibitor_id=x["id"],
                criteria={
                    "target_industries": rng.sample(list(ev.industries), 2),
                    "interests": tags,
                    "min_seniority": rng.choice(["senior", "manager", "director"]),
                    "notes": f"Prioritize teams actively evaluating {tags[0]} this year.",
                },
            )

    # --- people ---------------------------------------------------------------------------------

    def role(self, user: dict, role: str, exhibitor_id=None) -> None:
        self.add(
            "user_event_roles",
            id=self.g.uid(),
            user_id=user["id"],
            role=role,
            exhibitor_id=exhibitor_id,
        )

    def profile(self, user: dict, **over) -> dict:
        rng, ev = self.rng, self.ev
        seniority = rng.choice(SENIORITY)
        interests = rng.sample(self.interests, rng.randint(2, 4))
        looking = rng.sample(LOOKING_FOR, rng.randint(1, 2))
        p = dict(
            display_name=user["full_name"],
            job_title=rng.choice(JOB_TITLES[seniority]),
            company=self.fake.company(),
            industry=rng.choice(ev.industries),
            seniority=seniority,
            persona_type=rng.choice(PERSONA_TYPES),
            interests=interests,
            tags=[],
            looking_for=looking,
            offering=rng.sample(OFFERING, rng.randint(1, 2)),
            goals=f"Looking for {' and '.join(looking)} around {interests[0]}.",
            networking_opt_in=rng.random() < 0.65,
            exhibitor_matching_opt_in=rng.random() < 0.45,
        )
        p.update(over)
        p["tags"] = sorted({p["industry"], p["seniority"], *p["interests"][:2]})
        p["bio"] = (
            f"{p['job_title']} at {p['company']} ({p['industry']}). "
            f"Interested in {', '.join(p['interests'])}."
        )
        return self.add("attendee_profiles", id=self.g.uid(), user_id=user["id"], **p)

    def build_people(self) -> None:
        ev, g = self.ev, self.g
        for o in self.organizers:
            self.role(o, "organizer")
        # exhibitor staff: persona staff where defined, one generated staff member otherwise
        persona_staff = {p.exhibitor: p for p in self.personas if p.role == "exhibitor"}
        self.staff: dict[str, dict] = {}
        for name, x in self.exhibitors.items():
            if name in persona_staff:
                user = self.persona_users[persona_staff[name].key]
            else:
                user = g.new_user(self.tid, f"{_slug(name)}.example.com")
            self.role(user, "exhibitor", x["id"])
            self.staff[name] = user
        # attendees: personas, then ~40% returning tenant attendees (for the small event), new
        self.attendees: list[dict] = []
        self.profiles: dict[uuid.UUID, dict] = {}
        for p in self.personas:
            if p.role == "attendee":
                u = self.persona_users[p.key]
                self.role(u, "attendee")
                self.profiles[u["id"]] = self.profile(u, **p.profile)
                self.attendees.append(u)
        returning = self.rng.sample(self.pool, min(len(self.pool), int(ev.n_attendees * 0.4)))
        while len(self.attendees) < ev.n_attendees:
            u = returning.pop() if returning else g.new_user(self.tid, self.t.email_domain)
            if u in self.attendees:
                continue
            self.role(u, "attendee")
            self.profiles[u["id"]] = self.profile(u)
            self.attendees.append(u)
        self.pool.extend(
            u for u in self.attendees if u not in self.pool and not u["is_demo_persona"]
        )

    # --- program --------------------------------------------------------------------------------

    def build_program(self) -> None:
        ev, rng, g = self.ev, self.rng, self.g
        self.tracks = {
            name: self.add(
                "tracks",
                id=g.uid(),
                name=name,
                description=f"Sessions on {', '.join(tags)}.",
            )
            for name, tags in ev.tracks.items()
        }
        self.rooms = {
            name: self.add("rooms", id=g.uid(), name=name, floor=floor, capacity=cap)
            for name, floor, cap in ev.rooms
        }
        halls = [r for r in ev.rooms if "Lab" not in r[0]]
        labs = [r for r in ev.rooms if "Lab" in r[0]]
        self.speakers = []
        track_names = list(ev.tracks)
        for i in range(ev.n_speakers):
            track = track_names[i % len(track_names)]
            self.speakers.append(
                self.add(
                    "speakers",
                    id=g.uid(),
                    user_id=None,
                    full_name=self.fake.name(),
                    title=rng.choice(JOB_TITLES[rng.choice(SENIORITY[2:])]),
                    company=self.fake.company(),
                    bio=f"Practitioner and speaker on {', '.join(ev.tracks[track])}.",
                    topics=list(ev.tracks[track]),
                )
            )
        self.sessions: list[dict] = []
        self.dropoff_session = self.overbooked_session = None
        # planted titles are reserved so generated titles never collide with them
        titles: set[str] = {t for t in (ev.dropoff_title, ev.overbooked_title) if t}
        last_day = len(ev.days) - 1
        n = 0
        for di, (off, parallel) in enumerate(ev.days):
            for si, slot in enumerate(ev.slots):
                for pi in range(parallel):
                    track = track_names[n % len(track_names)]
                    n += 1
                    topic = rng.choice(ev.tracks[track])
                    if di == 0 and si == 0 and pi == 0:
                        kind, room = "keynote", halls[0]
                        title = KEYNOTE_TITLE.format(event=ev.name)
                    elif labs and si == 1 and pi == parallel - 1:
                        kind, room = "workshop", labs[di % len(labs)]
                        title = WORKSHOP_PATTERN.format(topic=_cap(topic))
                    else:
                        kind = rng.choices(["talk", "panel"], [0.75, 0.25])[0]
                        room = halls[1 + pi % (len(halls) - 1)] if len(halls) > 1 else halls[0]
                        pattern = PANEL_PATTERN if kind == "panel" else rng.choice(TITLE_PATTERNS)
                        title = pattern.format(topic=_cap(topic))
                    planted = None
                    if ev.dropoff_title and di == 0 and si == 3 and pi == 1:
                        planted, kind, title = "dropoff", "talk", ev.dropoff_title
                    elif ev.overbooked_title and di == last_day and kind == "workshop":
                        planted, title = "overbooked", ev.overbooked_title
                        room = labs[0]  # stable for demos: always the first lab
                    if planted is None and title in titles:
                        options = [
                            p.format(topic=_cap(t))
                            for t in ev.tracks[track]
                            for p in TITLE_PATTERNS
                        ]
                        rng.shuffle(options)
                        title = next((o for o in options if o not in titles), f"{title} (Part 2)")
                    titles.add(title)
                    starts = g.at(off, slot)
                    tags = sorted({topic, rng.choice(ev.tracks[track])})
                    s = self.add(
                        "sessions",
                        id=g.uid(),
                        track_id=self.tracks[track]["id"],
                        room_id=self.rooms[room[0]]["id"],
                        title=title,
                        description=(
                            f"A {kind} in the {track} track covering {topic}. "
                            f"Expect practical examples, open questions and time for Q&A."
                        ),
                        kind=kind,
                        starts_at=starts,
                        ends_at=starts + timedelta(minutes=DURATION_MIN[kind]),
                        capacity=room[2],
                        tags=tags,
                    )
                    self.sessions.append(s)
                    if planted == "dropoff":
                        self.dropoff_session = s
                    elif planted == "overbooked":
                        self.overbooked_session = s
                    n_spk = 3 if kind == "panel" else 1
                    pool = [sp for sp in self.speakers if set(sp["topics"]) & set(tags)]
                    for sp in rng.sample(
                        pool or self.speakers, min(n_spk, len(pool or self.speakers))
                    ):
                        self.add(
                            "session_speakers", id=g.uid(), session_id=s["id"], speaker_id=sp["id"]
                        )

    def build_sponsors(self) -> None:
        tiers = ["Platinum", "Gold", "Gold", "Silver", "Silver", "Silver", "Bronze", "Bronze"]
        n = 8 if self.ev.n_exhibitors >= 20 else 2
        for tier, x in zip(
            tiers[:n], self.rng.sample(list(self.exhibitors.values()), n), strict=True
        ):
            self.add(
                "sponsors",
                id=self.g.uid(),
                name=x["name"],
                tier=tier,
                description=f"{tier} sponsor of {self.ev.name}. {x['description']}",
                exhibitor_id=x["id"],
                website=x["website"],
            )

    # --- FAQs and documents ---------------------------------------------------------------------

    def build_content(self) -> None:
        ev = self.ev
        first_day = self.g.anchor + timedelta(days=ev.days[0][0])
        faqs = [
            ("What is the Wi-Fi?", f"Connect to {ev.wifi}.", "logistics", "public"),
            ("Where is the venue?", f"{ev.name} takes place at {ev.venue}.", "logistics", "public"),
            (
                "Where do I pick up my badge?",
                f"Badge pickup opens at 08:00 on {first_day:%B %d} in the main lobby. "
                "Bring a photo ID.",
                "registration",
                "public",
            ),
            (
                "Is lunch provided?",
                "Lunch is served 12:00-13:00 in the exhibit hall each day. Vegetarian, vegan and "
                "gluten-free options are labelled.",
                "food",
                "public",
            ),
            (
                "Are sessions recorded?",
                "Keynotes and main-hall talks are recorded and posted in the event app within "
                "48 hours. Workshops are not recorded.",
                "program",
                "public",
            ),
            (
                "Is the venue accessible?",
                "All rooms are step-free. Hearing loops are available in the main hall. Ask at the "
                "help desk for wheelchair loans.",
                "accessibility",
                "public",
            ),
            (
                "How do I connect with other attendees?",
                "Turn on networking in your profile. Only attendees who opted in appear in "
                "matches, and contact details are shared only after a connection is accepted.",
                "networking",
                "public",
            ),
            (
                "Where is the attendee lounge?",
                "The attendee lounge is next to the main hall, with coffee and charging stations. "
                "Your attendee badge is required.",
                "perks",
                "attendee",
            ),
            (
                "Do I get a certificate of attendance?",
                "Yes. Certificates are emailed to checked-in attendees within a week after the "
                "event.",
                "perks",
                "attendee",
            ),
            (
                "Who is the on-call operations lead?",
                "The on-call operations lead is reachable on the organizer radio channel 3 and at "
                "extension 4410.",
                "operations",
                "organizer_only",
            ),
            (
                "What is the VIP green room access code?",
                "The speaker green room code is 7319. "
                "Do not share it with attendees or exhibitors.",
                "operations",
                "organizer_only",
            ),
        ]
        for q, a, cat, vis in faqs:
            self.add(
                "faqs",
                id=self.g.uid(),
                question=q,
                answer=a,
                category=cat,
                visibility=vis,
                owner_exhibitor_id=None,
            )
        for name in ev.fixed_exhibitors:
            x = self.exhibitors[name]
            self.add(
                "faqs",
                id=self.g.uid(),
                question=f"What discount can {name} offer at the show?",
                answer=f"{name} booth staff may offer up to 15% off for deals signed within "
                "30 days of the event. Anything above needs sales director approval.",
                category="sales",
                visibility="exhibitor_private",
                owner_exhibitor_id=x["id"],
            )

        docs = [
            (
                "Venue guide",
                "public",
                None,
                f"{ev.name} venue guide. The event is held at {ev.venue}. "
                + " ".join(f"{r[0]} is on {r[1]} and seats {r[2]}." for r in ev.rooms)
                + " Cloakroom and help desk are in the main lobby. Quiet rooms are available on "
                "request. First aid is next to the help desk.",
            ),
            (
                "Code of conduct",
                "public",
                None,
                f"All participants of {ev.name} agree to the code of conduct. Harassment of any "
                "kind is not tolerated. Report concerns at the help desk or through the event app. "
                "Photography of attendees requires consent. Badge scanning by exhibitors requires "
                "the attendee's agreement.",
            ),
            (
                "Attendee perks",
                "attendee",
                None,
                f"Attendees of {ev.name} get access to the attendee lounge, recorded sessions for "
                "90 days, and a certificate of attendance. Workshops require a bookmark in the app "
                "because seats are limited.",
            ),
            (
                "Incident response runbook",
                "organizer_only",
                None,
                "Organizer-only runbook. Medical incidents: call venue security on extension 4400, "
                "then the on-call operations lead. Room overflow: open the overflow room and post "
                "an announcement in the app after organizer approval. Schedule changes must be "
                "approved by an organizer before attendees are notified.",
            ),
        ]
        for name in ev.fixed_exhibitors:
            x = self.exhibitors[name]
            rival = next((n for n in ev.fixed_exhibitors if n != name), None)
            body = (
                f"{name} internal pricing sheet. List price for the enterprise plan is $48,000 per "
                "year. Show special: 15% discount for deals signed within 30 days."
            )
            if rival:
                body += f" Competitive note: position against {rival} on integration speed."
            docs.append((f"{name} pricing sheet", "exhibitor_private", x["id"], body))
        for title, vis, owner, content in docs:
            self.add(
                "documents",
                id=self.g.uid(),
                title=title,
                source_type="text",
                content=content,
                visibility=vis,
                owner_exhibitor_id=owner,
            )

    # --- bookmarks & attendance -----------------------------------------------------------------

    def interest_overlap(self, user_id, tags) -> int:
        return len(set(self.profiles[user_id]["interests"]) & set(tags))

    def build_bookmarks(self) -> None:
        rng = self.rng
        self.bookmarks: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)  # session -> users
        for u in self.attendees:
            weights = [1 + 3 * self.interest_overlap(u["id"], s["tags"]) for s in self.sessions]
            for s in self.g.weighted_sample(self.sessions, weights, rng.randint(3, 8)):
                self.bookmarks[s["id"]].add(u["id"])
        if self.overbooked_session:
            s = self.overbooked_session
            target = min(int(s["capacity"] * 2.4), len(self.attendees))
            extra = [u["id"] for u in self.attendees if u["id"] not in self.bookmarks[s["id"]]]
            rng.shuffle(extra)
            while len(self.bookmarks[s["id"]]) < target and extra:
                self.bookmarks[s["id"]].add(extra.pop())
        for sid, users in self.bookmarks.items():
            for uid_ in users:
                self.add(
                    "bookmarks",
                    id=self.g.uid(),
                    user_id=uid_,
                    session_id=sid,
                    created_at=self.g.now - timedelta(days=rng.randint(3, 20)),
                )

    def log(self, user_id, kind, when, session_id=None, exhibitor_id=None, meta=None) -> None:
        self.add(
            "engagement_logs",
            user_id=user_id,
            kind=kind,
            session_id=session_id,
            exhibitor_id=exhibitor_id,
            occurred_at=when,
            meta=meta or {},
        )

    def build_attendance(self) -> None:
        rng, now = self.rng, self.g.now
        busy: dict[uuid.UUID, list[tuple[datetime, datetime]]] = defaultdict(list)

        def free(uid_, s) -> bool:
            return all(s["ends_at"] <= a or s["starts_at"] >= b for a, b in busy[uid_])

        for s in sorted(self.sessions, key=lambda s: s["starts_at"]):
            if s["ends_at"] > now:
                continue  # upcoming: bookmarks only
            cap = s["capacity"]
            going = [u for u in self.bookmarks[s["id"]] if rng.random() < 0.7 and free(u, s)]
            share = 0.5 if s["kind"] == "keynote" else rng.uniform(0.04, 0.15)
            if s is self.dropoff_session:
                share = 0.2
            others = [
                u["id"]
                for u in self.attendees
                if u["id"] not in self.bookmarks[s["id"]] and free(u["id"], s)
            ]
            going += rng.sample(others, min(len(others), int(share * len(self.attendees))))
            going = going[:cap]
            for u in going:
                busy[u].append((s["starts_at"], s["ends_at"]))
                t_in = s["starts_at"] + timedelta(minutes=rng.uniform(-5, 8))
                r = rng.random()
                if s is self.dropoff_session and r < 0.55:
                    t_out = s["starts_at"] + timedelta(minutes=rng.uniform(20.5, 26))
                elif r > 0.88:
                    lo = (t_in - s["starts_at"]).total_seconds() / 60 + 3
                    t_out = s["starts_at"] + timedelta(
                        minutes=rng.uniform(max(lo, 5), DURATION_MIN[s["kind"]] - 6)
                    )
                else:
                    t_out = s["ends_at"] + timedelta(minutes=rng.uniform(0, 4))
                self.log(u, "session_check_in", t_in, session_id=s["id"])
                self.log(u, "session_check_out", t_out, session_id=s["id"])

    # --- booths, leads, meetings ------------------------------------------------------------------

    def lead_score(self, user_id, exhibitor: dict, boost: int = 0) -> int:
        p = self.profiles[user_id]
        crit = next(
            c["criteria"]
            for c in self.g.rows["exhibitor_lead_criteria"]
            if c["exhibitor_id"] == exhibitor["id"]
        )
        score = 25 + 14 * len(set(p["interests"]) & set(crit["interests"]))
        score += 20 if p["industry"] in crit["target_industries"] else 0
        score += 4 * SENIORITY.index(p["seniority"])
        score += boost + self.rng.gauss(0, 8)
        return max(5, min(98, round(score)))

    def add_lead(self, user_id, x: dict, source: str, when: datetime, boost: int = 0) -> None:
        key = (x["id"], user_id)
        if key in self.lead_keys:
            return
        self.lead_keys.add(key)
        scored = self.rng.random() < 0.7
        score = self.lead_score(user_id, x, boost) if scored else None
        self.add(
            "leads",
            id=self.g.uid(),
            exhibitor_id=x["id"],
            attendee_user_id=user_id,
            source=source,
            captured_at=when,
            score=score,
            tier=_tier(score) if score is not None else None,
            notes=None,
        )

    def rand_time(self, day_off: int, start="10:00", minutes=450) -> datetime:
        return self.g.at(day_off, start, self.rng.uniform(0, minutes))

    def build_booths_and_leads(self) -> None:
        rng = self.rng
        self.lead_keys: set[tuple] = set()
        xs = list(self.exhibitors.values())
        if not self.past_days:
            return
        for u in self.attendees:
            weights = [1 + 3 * self.interest_overlap(u["id"], x["tags"]) for x in xs]
            for x in self.g.weighted_sample(xs, weights, rng.randint(1, 6)):
                when = self.rand_time(rng.choice(self.past_days))
                self.log(u["id"], "booth_visit", when, exhibitor_id=x["id"])
                if rng.random() < 0.28:
                    self.add_lead(u["id"], x, "badge_scan", when + timedelta(minutes=2))
        if self.ev.spike_exhibitor:
            x = self.exhibitors[self.ev.spike_exhibitor]
            day = max(self.past_days)  # yesterday
            for u in rng.sample(self.attendees, 55):
                if u["is_demo_persona"]:
                    continue
                when = self.rand_time(day, "13:00", 120)
                self.log(u["id"], "booth_visit", when, exhibitor_id=x["id"])
                if rng.random() < 0.8:
                    self.add_lead(u["id"], x, "badge_scan", when + timedelta(minutes=2), boost=15)

    def build_networking(self) -> None:
        rng, g = self.rng, self.g
        if not self.past_days:
            return
        sara = (
            self.persona_users.get("sara") if any(p.key == "sara" for p in self.personas) else None
        )
        # meeting requests: only attendees who opted in to exhibitor matching
        matchable = [
            u for u in self.attendees if self.profiles[u["id"]]["exhibitor_matching_opt_in"]
        ]
        xs = list(self.exhibitors.values())

        def meeting(u, x, status, initiated_by, created):
            responded = (
                created + timedelta(hours=rng.uniform(1, 6)) if status != "pending" else None
            )
            self.add(
                "meeting_requests",
                id=g.uid(),
                attendee_user_id=u["id"],
                exhibitor_id=x["id"],
                initiated_by=initiated_by,
                status=status,
                proposed_at=created + timedelta(days=1),
                message=f"Would love to discuss {x['tags'][0]}.",
                created_at=created,
                responded_at=responded,
            )
            if status == "accepted":
                self.add_lead(u["id"], x, "meeting_accepted", responded)

        if sara:
            meeting(sara, self.exhibitors["MedScan"], "accepted", "attendee", g.at(-2, "15:00"))
            meeting(sara, self.exhibitors["CardioVue"], "pending", "exhibitor", g.at(-1, "10:00"))
        for _ in range(int(len(self.attendees) * 0.3)):
            u = rng.choice(matchable)
            if u is sara:
                continue
            weights = [1 + 3 * self.interest_overlap(u["id"], x["tags"]) for x in xs]
            x = rng.choices(xs, weights)[0]
            status = rng.choices(["accepted", "pending", "declined"], [0.5, 0.25, 0.25])[0]
            meeting(
                u,
                x,
                status,
                rng.choice(["attendee", "exhibitor"]),
                self.rand_time(rng.choice(self.past_days), "09:00", 480),
            )

        # peer connections: only between attendees who opted in to networking
        open_ = [u for u in self.attendees if self.profiles[u["id"]]["networking_opt_in"]]
        seen: set[frozenset] = set()

        def connect(a, b, status, created):
            key = frozenset((a["id"], b["id"]))
            if a is b or key in seen:
                return
            seen.add(key)
            self.add(
                "connections",
                id=g.uid(),
                requester_id=a["id"],
                addressee_id=b["id"],
                status=status,
                message="Great to meet you at the event!",
                created_at=created,
                responded_at=created + timedelta(hours=2) if status != "pending" else None,
            )

        if sara:
            peers = [u for u in open_ if u is not sara]
            connect(sara, peers[0], "accepted", g.at(-2, "11:00"))
            connect(peers[1], sara, "accepted", g.at(-2, "16:30"))
            connect(peers[2], sara, "pending", g.at(-1, "09:30"))
        for _ in range(int(len(open_) * 1.4)):
            a, b = rng.sample(open_, 2)
            if sara in (a, b):
                continue
            status = rng.choices(["accepted", "pending", "declined"], [0.6, 0.25, 0.15])[0]
            connect(a, b, status, self.rand_time(rng.choice(self.past_days), "09:00", 540))

    def build_app_activity(self) -> None:
        rng = self.rng
        targets = ["session", "exhibitor", "sponsor", "speaker"]
        for u in self.attendees:
            for day in self.past_days:
                for _ in range(rng.randint(1, 2)):
                    self.log(u["id"], "app_open", self.rand_time(day, "08:00", 600))
                for _ in range(rng.randint(0, 4)):
                    self.log(
                        u["id"],
                        "click",
                        self.rand_time(day, "08:00", 600),
                        meta={"target": rng.choice(targets)},
                    )

    def build_policies(self) -> None:
        for action, role, bulk, escalate, hours in (
            ("exhibitor_follow_up_email", "exhibitor", 20, "organizer", 48),
            ("schedule_change", "organizer", None, None, 12),
            ("attendee_announcement", "organizer", None, None, 12),
            ("session_summary_publish", "organizer", None, None, 72),
        ):
            self.add(
                "approval_policies",
                id=self.g.uid(),
                action_type=action,
                approver_role=role,
                bulk_threshold=bulk,
                escalate_to_role=escalate,
                expiry_hours=hours,
            )


def generate(anchor: date | None = None, seed: int = 42) -> Rows:
    return Generator(anchor or datetime.now(UTC).date(), seed).build()
