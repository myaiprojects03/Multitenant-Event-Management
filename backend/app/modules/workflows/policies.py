"""Who approves what. Policies are rows in approval_policies (seeded per event); the chain of
approvals is computed in code from the policy and the *final* draft, so editing a draft into
something riskier (more recipients, commercial terms, outside links) re-triggers escalation.
"""

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Policy:
    action_type: str
    approver_role: str
    bulk_threshold: int | None
    escalate_to_role: str | None
    expiry_hours: int


class PolicyMissing(Exception):
    pass


async def load_policy(db: AsyncSession, action_type: str) -> Policy:
    row = (
        (
            await db.execute(
                text(
                    "SELECT action_type, approver_role, bulk_threshold, escalate_to_role, "
                    "expiry_hours FROM approval_policies WHERE action_type = :a"
                ),
                {"a": action_type},
            )
        )
        .mappings()
        .first()
    )
    if row is None:  # fail closed: no policy, no workflow
        raise PolicyMissing(f"no approval policy for {action_type} in this event")
    return Policy(**row)


# Things an exhibitor may not promise or link to without an organizer looking at it.
_COMMERCIAL = re.compile(
    r"\d+\s*%|[$€£]\s?\d|\b(discount|free of charge|complimentary|rebate|guarantee[ds]?|"
    r"exclusive (deal|offer|pricing))\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://([^/\s]+)|www\.([^/\s]+)", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")


def flag_reasons(body: str, allowed_domains: set[str]) -> list[str]:
    """Why a message needs a second look. Pure; used on drafts and again on edits."""
    flags = []
    if _COMMERCIAL.search(body or ""):
        flags.append("commercial terms (price, discount or guarantee)")
    domains = {(a or b).lower().rstrip(".,)") for a, b in _URL.findall(body or "")}
    domains |= {d.lower().rstrip(".,)") for d in _EMAIL.findall(body or "")}
    outside = sorted(
        d for d in domains if not any(d == a or d.endswith("." + a) for a in allowed_domains)
    )
    if outside:
        flags.append("link or address outside the exhibitor's domain: " + ", ".join(outside))
    return flags


def approval_chain(policy: Policy, n_recipients: int, flagged: bool) -> list[str]:
    """Roles that must approve, in order. The owner always approves first."""
    chain = [policy.approver_role]
    needs_second = flagged or (
        policy.bulk_threshold is not None and n_recipients > policy.bulk_threshold
    )
    if needs_second and policy.escalate_to_role and policy.escalate_to_role not in chain:
        chain.append(policy.escalate_to_role)
    return chain
