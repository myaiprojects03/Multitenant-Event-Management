"""The eval harness's own scoring logic (pure, no DB): a lenient scorer would hide failures."""

import uuid

from app.core.context import RequestContext
from evals.harness import Harness, Login, contains, fill, load, percentile
from evals.results import sample
from evals.suites.common import can_see, leaked
from evals.suites.nl_sql import compare


def test_golden_files_parse_and_ids_are_unique():
    for name, key in [
        ("rag.yaml", "cases"),
        ("rbac.yaml", "cases"),
        ("nl_sql.yaml", "cases"),
        ("workflows.yaml", "scenarios"),
    ]:
        ids = [c["id"] for c in load(name)[key]]
        assert ids and len(ids) == len(set(ids)), name
    safety = load("sql_safety.yaml")
    assert len(safety["guard"]["unsafe"]) >= 15 and safety["prompt"]


def test_nl_sql_compare_ignores_names_but_not_values():
    ref = ([("title")], [{"title": "A"}, {"title": "B"}])
    assert compare(
        ["title"], ref[1], ["name", "n"], [{"name": "b", "n": 1}, {"name": "a", "n": 2}], "rows"
    )[0]
    assert not compare(["title"], ref[1], ["name"], [{"name": "A"}, {"name": "C"}], "rows")[0]
    # ordered: same values, wrong order fails
    assert not compare(["t"], [{"t": "A"}, {"t": "B"}], ["t"], [{"t": "B"}, {"t": "A"}], "ordered")[
        0
    ]
    # numbers within 1%, rounding differences tolerated
    assert compare(["v"], [{"v": 33.333}], ["x"], [{"x": 33.3}], "rows")[0]
    assert not compare(["v"], [{"v": 40}], ["x"], [{"x": 44}], "rows")[0]
    # first: only the top row matters, extra rows are fine
    assert compare(
        ["t"],
        [{"t": "MedScan"}],
        ["t", "n"],
        [{"t": "MedScan", "n": 9}, {"t": "X", "n": 1}],
        "first",
    )[0]
    assert not compare(["t"], [{"t": "MedScan"}], ["t"], [{"t": "X"}], "first")[0]
    # dates vs timestamps
    assert compare(
        ["d"], [{"d": "2026-11-25"}], ["d"], [{"d": "2026-11-25T00:00:00+00:00"}], "rows"
    )[0]


def test_can_see_mirrors_the_database_rule():
    x = uuid.uuid4()

    def login(role, exhibitor=None):
        ctx = RequestContext(uuid.uuid4(), uuid.uuid4(), role, uuid.uuid4(), exhibitor)
        return Login("p", "e", "t", ctx, "ev")

    att, org, mine, other = (
        login("attendee"),
        login("organizer"),
        login("exhibitor", x),
        login("exhibitor", uuid.uuid4()),
    )
    assert can_see("public", None, att) and can_see("attendee", None, att)
    assert not can_see("attendee", None, mine)
    assert can_see("exhibitor_private", x, mine) and not can_see("exhibitor_private", x, other)
    assert not can_see("exhibitor_private", x, att) and can_see("exhibitor_private", x, org)
    assert can_see("organizer_only", None, org) and not can_see("organizer_only", None, mine)
    assert not can_see("unknown", None, org)


def test_text_helpers():
    assert contains("Room  101 (Level 1)", "room 101")
    assert contains("It’s at 10:30", "it's at 10:30")
    assert fill("Where is {title}?", {"title": "X"}) == "Where is X?"
    assert fill("{missing}", {}) == "{missing}"
    assert leaked("code is 7319", ["7319"], []) == ["7319"]
    assert leaked("mail bob@x.example.com", [], [r"[\w.]+@[\w-]+\.example\.com"])
    assert leaked("nothing here", ["7319"], [r"@\w+"]) == []


def test_sampling_spreads_and_percentiles():
    assert sample(list(range(10)), 3) == [0, 3, 6]
    assert sample([1, 2], None) == [1, 2]
    assert percentile([1, 2, 3, 4], 0.5) == 2.5 and percentile([], 0.5) is None


def test_forged_token_keeps_signature_changes_claims():
    import base64
    import json

    claims = base64.urlsafe_b64encode(
        json.dumps({"role": "attendee", "tid": "t"}).encode()
    ).decode()
    forged = Harness.forged(f"h.{claims.rstrip('=')}.sig")
    head, payload, sig = forged.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert (head, sig) == ("h", "sig") and body["role"] == "organizer"
