"""AI inventory card: one card saying what this AI is.

You cannot protect what you have not listed. The card is filled automatically
from the latest candidate fingerprint, so a model swap updates it rather than
leaving a stale hand-typed entry.
"""
from __future__ import annotations

from lab.governance.candidate import Fingerprint


def build_card(fp: Fingerprint, policy: dict) -> dict:
    """Build the inventory card from a fingerprint and the policy."""
    return {
        "system": "Northwind IT support agent",
        "owner": "IT Operations (synthetic)",
        "purpose": "answer staff IT questions; read docs, look up assets, open tickets",
        "model": fp.model,
        "model_digest": fp.model_digest,
        "tools": list(fp.tools),
        "defences": list(fp.defences) or ["none"],
        "data": "synthetic only; one restricted record (VAULT-01) holding a fake secret",
        "risk_tier": "high",  # can read restricted data and take actions
        "policy_version": policy.get("policy_version", "unknown"),
        "candidate_id": fp.candidate_id,
    }


def to_markdown(card: dict) -> str:
    """Render the inventory card as a Markdown list."""
    lines = ["**AI inventory card**", ""]
    for key, value in card.items():
        shown = ", ".join(value) if isinstance(value, list) else value
        lines.append(f"- **{key.replace('_', ' ')}:** {shown}")
    return "\n".join(lines)
