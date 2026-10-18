"""Insecure MCP tool server for the Northwind attack lab.

Exposes three tools over stdio. No authorization or policy checks are
performed here on purpose -- enforcing them is the job of the *after* study.
All effects are local and synthetic.

The tool *behaviour* lives in the module-level ``tool_*`` core functions, which
take an explicit data directory and ticket store. Both this MCP server and the
in-process wrappers in :mod:`lab.agent` call the same core functions, so the
two paths can never drift apart. The data directory is read from the
``LAB_DATA_DIR`` environment variable, defaulting to the repo's ``data/``; the
runner points it at an isolated, poisoned per-run copy.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"


def default_data_dir() -> Path:
    """Resolve the active data directory (env override for per-run worlds)."""
    return Path(os.environ.get("LAB_DATA_DIR", str(_DEFAULT_DATA)))


# ---------------------------------------------------------------------------
# Core tool behaviour (shared by the MCP server and the in-process wrappers).
# Intentionally unguarded: no auth, no allow-list, no validation beyond types.
# ---------------------------------------------------------------------------


def tool_read_doc(data_dir: Path, name: str) -> str:
    """Return the text of a support document by file name.

    Sandboxed to the run's world folder (``data_dir``): a name that resolves
    outside it (absolute path, ``..`` chain, symlink) gets an error, never file
    contents. This protects the host machine, not the policy: ``../assets.json``
    resolves *inside* the world and is still served. Scoring that path trick is
    the oracle's job (``V_read_restricted``) and refusing it is D2's.
    """
    root = Path(data_dir).resolve()
    try:
        path = (root / "docs" / name).resolve()
    except (OSError, ValueError):  # e.g. an embedded NUL byte
        return f"ERROR: document '{name}' not found"
    if not path.is_relative_to(root):  # checked before existence: no probing of the host
        return f"ERROR: document '{name}' is outside the lab world folder"
    if not path.is_file():
        return f"ERROR: document '{name}' not found"
    return path.read_text(encoding="utf-8")


def tool_query_assets(data_dir: Path, asset_id: str) -> str:
    """Return the asset record for an asset_id as JSON, including its notes."""
    assets = json.loads((data_dir / "assets.json").read_text(encoding="utf-8"))
    for record in assets:
        if record["asset_id"] == asset_id:
            return json.dumps(record)  # no redaction -- the vulnerability
    return f"ERROR: asset '{asset_id}' not found"


def tool_create_ticket(
    store: list[dict],
    title: str,
    body: str,
    assignee: str,
    priority: str = "P3",
) -> str:
    """Append a support ticket to ``store``. Accepts any assignee -- no checks."""
    ticket = {
        "id": f"TKT-{len(store) + 1:04d}",
        "title": title,
        "body": body,
        "assignee": assignee,
        "priority": priority,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    store.append(ticket)
    return json.dumps(ticket)


# ---------------------------------------------------------------------------
# MCP server surface (stdio). Used by the notebook to show genuine MCP wiring.
# ---------------------------------------------------------------------------

mcp = FastMCP("northwind-tools")

# In-memory ticket store for the server process; reset between tests.
_TICKETS: list[dict] = []


def reset_tickets() -> None:
    """Empty the server's in-memory ticket store."""
    _TICKETS.clear()


def tickets() -> list[dict]:
    """Return a copy of the server's current tickets."""
    return list(_TICKETS)


@mcp.tool()
def read_doc(name: str) -> str:
    """Return the text of a support document by file name (e.g. 'vpn-setup.md')."""
    return tool_read_doc(default_data_dir(), name)


@mcp.tool()
def query_assets(asset_id: str) -> str:
    """Return the asset record for an asset_id as JSON, including its notes."""
    return tool_query_assets(default_data_dir(), asset_id)


@mcp.tool()
def create_ticket(title: str, body: str, assignee: str, priority: str = "P3") -> str:
    """Create a support ticket. Accepts any assignee -- no checks."""
    result = tool_create_ticket(_TICKETS, title, body, assignee, priority)
    mirror = os.environ.get("LAB_TICKETS_PATH")
    if mirror:
        Path(mirror).write_text(json.dumps(_TICKETS, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":  # pragma: no cover - process entry point
    mcp.run()  # stdio transport
