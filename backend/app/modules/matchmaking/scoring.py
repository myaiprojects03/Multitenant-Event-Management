"""Pure scoring helpers: feature overlap, complementarity, feedback weights, diversity."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

# looking_for -> what a match should offer
NEED_TO_OFFER = {
    "mentorship": {"mentoring"},
    "investors": {"funding"},
    "hiring": {"engineering talent"},
    "partners": {"integration experience", "pilot site", "customer introductions"},
    "customers": {"customer introductions", "pilot site"},
    "research collaborators": {"research data"},
    "peer exchange": {"domain expertise"},
}
COMPLEMENTARY = {
    ("buyer", "seller"), ("seller", "buyer"), ("mentor", "mentee"), ("mentee", "mentor"),
    ("investor", "seller"), ("seller", "investor"),
}  # fmt: skip
SENIORITY = ("junior", "mid", "senior", "manager", "director", "executive")

DIVERSITY_PENALTY = 0.08  # per already-selected item with the same category
POSITIVE_KINDS = ("click", "save", "connect")


def scaled_similarity(sim: float) -> float:
    """bge-small cosine similarities for related texts sit around 0.6-0.95; map to 0..1."""
    return max(0.0, min(1.0, (sim - 0.6) / 0.35))


def overlap(a: list[str] | None, b: list[str] | None) -> list[str]:
    bl = {x.lower() for x in (b or [])}
    return [x for x in (a or []) if x.lower() in bl]


def jaccard(a, b) -> float:
    sa, sb = {x.lower() for x in a or []}, {x.lower() for x in b or []}
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def needs_met(looking_for: list[str], offering: list[str]) -> list[tuple[str, str]]:
    met = []
    for need in looking_for or []:
        for offer in offering or []:
            if offer in NEED_TO_OFFER.get(need, set()):
                met.append((need, offer))
    return met


def seniority_at_least(level: str | None, minimum: str | None) -> bool:
    if not level or not minimum or level not in SENIORITY or minimum not in SENIORITY:
        return False
    return SENIORITY.index(level) >= SENIORITY.index(minimum)


def human_list(items: list[str], n: int = 2) -> str:
    items = items[:n]
    return items[0] if len(items) == 1 else " and ".join([", ".join(items[:-1]), items[-1]])


@dataclass
class Feedback:
    """Per-user signals from recommendation_events."""

    impressions: Counter = field(default_factory=Counter)  # (type, id) -> n
    positives: Counter = field(default_factory=Counter)  # (type, id) -> n
    category_score: dict = field(default_factory=lambda: defaultdict(float))  # (type, cat)

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "Feedback":
        fb = cls()
        for r in rows:
            key = (r["target_type"], r["target_id"])
            cat = (r["target_type"], r["category"])
            n = r["n"]
            if r["kind"] == "impression":
                fb.impressions[key] += n
            elif r["kind"] in POSITIVE_KINDS:
                fb.positives[key] += n
                fb.category_score[cat] += 0.04 * n
            elif r["kind"] == "dismiss":
                fb.category_score[cat] -= 0.06 * n
        return fb

    def adjustment(self, target_type: str, target_id, category: str | None) -> float:
        cat = max(-0.25, min(0.25, self.category_score.get((target_type, category), 0.0)))
        key = (target_type, target_id)
        ignored = self.impressions[key] if not self.positives[key] else 0
        # shown more than twice and never engaged with: push it down
        ignore_penalty = min(0.25, max(0, ignored - 2) * 0.04)
        return cat - ignore_penalty


def diversify(items: list, k: int, category=lambda x: x.category, score=lambda x: x.score) -> list:
    """Greedy re-rank: each pick is penalized by how many of its category are already chosen."""
    pool, chosen, seen = list(items), [], Counter()
    while pool and len(chosen) < k:
        best = max(pool, key=lambda x: score(x) - DIVERSITY_PENALTY * seen[category(x)])
        pool.remove(best)
        chosen.append(best)
        seen[category(best)] += 1
    return chosen
