"""scripts/run_models.py: several Ollama models through the governance pipeline, offline.

Nothing here calls Ollama or a real model. Three kinds of child process are used:

* the repo's own ``scripts/run_governance.py`` in SCRIPTED mode (DEMO, no ``--live``),
  with its output sent to a tmp folder;
* small stand-in scripts in a tmp "fake repo" (same flags, no model) for everything that
  needs a LIVE-shaped run: run folders, traces, timing, Ctrl-C;
* no ``ollama`` binary at all (the pull step is pointed at a stand-in).

The real ``results/`` folder of the repo is never written.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from lab import run_folder

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def rm():
    spec = importlib.util.spec_from_file_location("run_models_cli", ROOT / "scripts" / "run_models.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_models_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# config/models.txt and the slug.
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str, *, raw: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_models_file_skips_comments_and_blank_lines(rm, tmp_path):
    f = _write(tmp_path / "models.txt", "# header\n\nqwen2.5:7b-instruct\n   # indented comment\n\nllama3.1:8b\n")
    models = rm.read_models(f)
    assert [m.tag for m in models] == ["qwen2.5:7b-instruct", "llama3.1:8b"]
    assert [m.slug for m in models] == ["qwen2.5-7b-instruct", "llama3.1-8b"]


def test_models_file_accepts_windows_editors(rm, tmp_path):
    # Notepad: UTF-8 BOM and CRLF line ends. A trailing "# note" after a tag is also allowed.
    raw = b"\xef\xbb\xbf# models\r\nqwen2.5:7b-instruct\r\n\r\nllama3.1:8b  # second\r\n"
    f = _write(tmp_path / "models.txt", "", raw=raw)
    assert [m.tag for m in rm.read_models(f)] == ["qwen2.5:7b-instruct", "llama3.1:8b"]


def test_models_file_refuses_duplicates(rm, tmp_path):
    f = _write(tmp_path / "models.txt", "qwen2.5:7b-instruct\n# again below\nllama3.1:8b\nqwen2.5:7b-instruct\n")
    with pytest.raises(rm.ModelsFileError) as exc:
        rm.read_models(f)
    msg = str(exc.value)
    assert "duplicate" in msg and "qwen2.5:7b-instruct" in msg
    assert "line 4" in msg and "line 1" in msg  # says where both copies are


def test_models_file_refuses_two_tags_that_share_a_slug(rm, tmp_path):
    # "a:b" and "a-b" would both write to <stamp>_a-b and share one run-folder name.
    f = _write(tmp_path / "models.txt", "tiny:1b\ntiny-1b\n")
    with pytest.raises(rm.ModelsFileError) as exc:
        rm.read_models(f)
    assert "slug" in str(exc.value) and "tiny-1b" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [
        "two tags on one line",
        "-leading-dash",              # would read as an option on the command line
        "semi;colon",
        "back`tick",
        "$(whoami)",
        "caf\u00e9:7b",
    ],
)
def test_models_file_refuses_odd_tags(rm, tmp_path, bad):
    f = _write(tmp_path / "models.txt", f"qwen2.5:7b-instruct\n{bad}\n")
    with pytest.raises(rm.ModelsFileError) as exc:
        rm.read_models(f)
    assert "line 2" in str(exc.value)


@pytest.mark.parametrize("tag", ["qwen2.5:7b-instruct", "llama3.1:8b-instruct-q4_K_M", "hf.co/user/repo:Q4_K_M", "granite3.3:8b"])
def test_models_file_accepts_real_looking_tags(rm, tmp_path, tag):
    f = _write(tmp_path / "models.txt", tag + "\n")
    assert [m.tag for m in rm.read_models(f)] == [tag]


def test_models_file_with_no_models_is_an_error(rm, tmp_path):
    f = _write(tmp_path / "models.txt", "# nothing here\n\n")
    with pytest.raises(rm.ModelsFileError, match="no models"):
        rm.read_models(f)


def test_models_file_missing_is_an_error(rm, tmp_path):
    with pytest.raises(rm.ModelsFileError, match="not found"):
        rm.read_models(tmp_path / "nope.txt")


def test_shipped_models_file(rm):
    """config/models.txt: the reference model, and a Granite note that is NOT a guessed tag."""
    path = ROOT / "config" / "models.txt"
    models = rm.read_models(path)
    assert [m.tag for m in models] == ["qwen2.5:7b-instruct"]
    text = path.read_text(encoding="utf-8")
    assert "# IBM Granite: tag not verified; check it with \"ollama pull <tag>\" before adding it" in text
    live = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("granite" in ln.lower() for ln in live), "a Granite tag must not be guessed"


@pytest.mark.parametrize(
    "text",
    ["qwen2.5:7b-instruct", "llama3.1:8b-instruct-q4_K_M", "hf.co/user/repo:Q4_K_M", "a b", "x" * 3, "", "\u00e9\u4e2d:1", "a/b\\c", "..", "C:\\x"],
)
def test_slug_is_the_run_folder_slug(rm, text):
    """The batch finds its run folders by this slug, so it must equal lab.run_folder._safe."""
    assert rm.safe_slug(text) == run_folder._safe(text)
