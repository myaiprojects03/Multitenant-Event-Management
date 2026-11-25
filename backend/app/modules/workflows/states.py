r"""Workflow state machine. Every status change goes through `check_transition`.

pending -> running -> awaiting_approval -> approved -> executing -> completed
                 \-> completed (nothing to do)   \-> rejected | expired    \-> failed
failed -> executing is allowed only for an organizer re-driving dead-lettered actions.
"""

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"awaiting_approval", "completed", "failed"}),
    "awaiting_approval": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"executing"}),
    "executing": frozenset({"completed", "failed"}),
    "failed": frozenset({"executing"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "completed": frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"workflow is {current}; cannot move to {target}")
        self.current, self.target = current, target


def check_transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(current, target)
