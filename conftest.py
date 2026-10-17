"""Ensure the repo root is importable so `pytest` works from any directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
