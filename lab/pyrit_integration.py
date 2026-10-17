"""Drive the Northwind attack lab through Microsoft PyRIT.

This is an *alternative front-end* to :mod:`lab.runner`: instead of our own loop,
the attack is delivered and recorded through PyRIT's abstractions.

* **Target** -- :class:`NorthwindAgentTarget` is a genuine PyRIT ``PromptTarget``.
  The prompt it receives is the *poison payload*; it plants that payload in an
  isolated per-run copy of the world (a document, a comment, or an asset note),
  runs the insecure agent on the benign user task, and returns the agent's final
  text. The ordered tool-call trace is attached to the response metadata.
* **Memory** -- every prompt/response is logged to PyRIT's in-memory store, so
  the whole red-team exchange is inspectable the PyRIT way.
* **Converters** -- PyRIT's ``Base64Converter``/``ROT13Converter`` etc. can
  generate encoded payload variants (see :func:`converter_for_technique`),
  demonstrating PyRIT producing the attacks.
* **Scoring** -- kept deterministic via :mod:`lab.oracle` (the spec mandates a
  reproducible, no-LLM oracle). The driver writes the *same* trace JSON format
  as :mod:`lab.runner`, so :mod:`lab.metrics`, the CSVs, and the chart all work
  unchanged whichever front-end produced the traces.

Run it with the combined environment (``.venv-pyrit``), which has both PyRIT and
the lab's own dependencies.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pyrit.converter import Base64Converter, LeetspeakConverter, ROT13Converter
from pyrit.models import Message
from pyrit.models.messages.conversations import construct_response_from_request
from pyrit.prompt_target import PromptTarget
from pyrit.setup import IN_MEMORY, initialize_pyrit_async

from lab import agent, oracle, run_folder, runner, world

# Metadata keys carried on the PyRIT prompt so the target can rebuild the world.
META_KEYS = ("test_id", "kind", "goal", "technique", "place", "user_task")


class NorthwindAgentTarget(PromptTarget):
    """A PyRIT target that runs the insecure Northwind agent against a payload.

    The incoming prompt is the (possibly converter-transformed) poison. Its
    ``prompt_metadata`` carries where to plant it and the benign user task to
    run. The response text is the agent's final answer; the ordered tool-call
    trace is returned in the response metadata as a JSON string.
    """

    def __init__(self, *, temperature: float = 0.2) -> None:
        super().__init__()
        self._temperature = temperature

    async def _send_prompt_to_target_async(
        self, *, normalized_conversation: list[Message]
    ) -> list[Message]:
        message = normalized_conversation[-1]
        request_piece = message.message_pieces[0]
        poison = message.get_value()
        meta = dict(request_piece.prompt_metadata or {})

        # Reconstruct the inject target from the carried metadata.
        test = {
            "inject_target": json.loads(meta["inject_target"]),
            "user_task": meta["user_task"],
        }
        user_task = meta["user_task"]

        with tempfile.TemporaryDirectory(prefix="pyrit-world-") as tmp:
            # The payload may have been transformed by a PyRIT converter, so
            # override the stored payload with what actually arrived.
            inject = json.loads(meta["inject_target"])
            _overwrite_payload(inject, poison)
            test["inject_target"] = inject
            world_dir = runner.build_world_dir(test, Path(tmp))
            tools, _store = agent.make_local_tools(world_dir)
            result = agent.run_task(
                agent.build_agent(tools, temperature=self._temperature), user_task
            )

        response_meta = {
            "tool_calls": json.dumps(result.tool_calls),
            "agent_error": result.error or "",
        }
        return [
            construct_response_from_request(
                request=request_piece,
                response_text_pieces=[result.final_text or "(no text)"],
                prompt_metadata=response_meta,
            )
        ]

    def _validate_request(self, *, normalized_conversation: list[Message]) -> None:
        last = normalized_conversation[-1]
        meta = last.message_pieces[0].prompt_metadata or {}
        missing = [k for k in ("inject_target", "user_task") if k not in meta]
        if missing:
            raise ValueError(f"prompt metadata missing {missing}")

    async def cleanup_target_async(self) -> None:  # pragma: no cover - nothing to free
        return None


def _overwrite_payload(inject_target: dict, payload: str) -> None:
    """Replace the payload inside an inject_target with ``payload`` in place.

    A no-op for benign records (empty dict, no ``type``). doc_split keeps its two
    original fragments, since a single converted string cannot be re-split.
    """
    kind = inject_target.get("type")
    if kind in ("doc_body", "doc_comment", "asset_notes"):
        inject_target["payload"] = payload


def converter_for_technique(technique: str):
    """Map a technique to a PyRIT converter, or ``None`` to keep the payload.

    Demonstrates PyRIT generating attack variants: the encoded technique is
    produced by PyRIT's ``Base64Converter`` rather than hand-rolled base64.
    """
    return {
        "T5_encoded": Base64Converter(),
    }.get(technique)


# Extra converters offered in the notebook to show PyRIT's payload generation.
DEMO_CONVERTERS = {
    "base64": Base64Converter(),
    "rot13": ROT13Converter(),
    "leetspeak": LeetspeakConverter(),
}


async def ensure_memory() -> None:
    """Initialise PyRIT's in-memory store (idempotent, safe to call repeatedly)."""
    await initialize_pyrit_async(IN_MEMORY, silent=True)


async def run_record(target: NorthwindAgentTarget, test: dict) -> dict:
    """Send one corpus record through PyRIT and return a runner-compatible trace."""
    meta = {
        "inject_target": json.dumps(test.get("inject_target") or {}),
        "user_task": test["user_task"],
    }
    message = Message.from_prompt(
        prompt=test.get("poison") or test["user_task"],
        role="user",
        prompt_metadata=meta,
    )
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    responses = await target.send_prompt_async(message=message)
    response = responses[0]
    piece = response.message_pieces[0]
    rmeta = piece.prompt_metadata or {}
    tool_calls = json.loads(rmeta.get("tool_calls", "[]"))
    final_text = response.get_value()
    return {
        "test_id": test["id"],
        "kind": test["kind"],
        "goal": test.get("goal"),
        "technique": test.get("technique", "none"),
        "place": test.get("place", "none"),
        "model": agent.MODEL,
        "temperature": target._temperature,
        "user_task": test["user_task"],
        "expect_ticket": test.get("expect_ticket", False),
        "expect_asset": test.get("expect_asset"),
        "tool_calls": tool_calls,
        "final_text": final_text,
        "error": rmeta.get("agent_error") or None,
        "started_at": started,
        "duration_s": round(time.time() - t0, 2),
        "driver": "pyrit",
    }


async def run_split(
    split_records: list[dict],
    reps: int = 3,
    *,
    temperature: float = 0.2,
    candidate: str = "adhoc",
    split: str | None = None,
    runs_root: Path | str | None = None,
    run_id: str | None = None,
    digest: str | None = None,
) -> Iterator[dict]:
    """Run a whole split through PyRIT, writing one trace per run (like the runner).

    Returns the list of traces (async generators are awkward to consume in a
    notebook loop, so this collects and returns).

    Like :func:`lab.runner.run_split`, every call writes into its own locked
    ``results/runs/<run_id>/`` folder (manifest with the model digest first,
    one trace per run, CSVs and hashes at lock time) and never deletes or
    overwrites anything. ``digest`` defaults to a best-effort Ollama lookup.
    """
    await ensure_memory()
    target = NorthwindAgentTarget(temperature=temperature)
    digest = runner.model_digest() if digest is None else digest
    run_dir = run_folder.create(
        list(split_records), model=agent.MODEL, model_digest=digest, driver="pyrit",
        reps=reps, temperature=temperature, seed=None, candidate=candidate,
        split=split, runs_root=runs_root, run_id=run_id,
    )
    traces: list[dict] = []
    try:
        for test in split_records:
            for rep in range(1, reps + 1):
                trace = await run_record(target, test)
                trace["rep"] = rep
                trace["run_id"] = run_dir.name
                trace["model_digest"] = digest
                run_folder.write_trace(run_dir, trace)
                traces.append(trace)
    finally:
        run_folder.lock_run(run_dir)
    return traces
