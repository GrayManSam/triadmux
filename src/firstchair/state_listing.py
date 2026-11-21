"""Read workflow metadata without returning task or result content."""

import json
import os
import stat
from pathlib import Path

from .sessions import SESSION_PATTERN
from .workflow import record_path

_STATUSES = ("running", "completed", "failed", "interrupted")
_STAGES = ("plan", "review", "synthesize")


def _read_summary(path: Path) -> dict[str, str] | None:
    run_id = path.stem
    try:
        if path != record_path(path.parent, run_id):
            return None
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            record = json.load(stream)
    except (OSError, RecursionError, ValueError):
        return None

    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
        or record.get("run_id") != run_id
        or record.get("status") not in _STATUSES
        or record.get("stage") not in _STAGES
    ):
        return None
    summary = {"run_id": run_id, "status": record["status"], "stage": record["stage"]}
    owner = record.get("session")
    if isinstance(owner, str) and SESSION_PATTERN.fullmatch(owner):
        summary["session"] = owner
    return summary


def list_runs(state_dir: Path) -> list[dict[str, str]]:
    """Return known run summaries sorted by canonical run ID."""
    try:
        paths = sorted(state_dir.iterdir())
    except FileNotFoundError:
        return []
    summaries = []
    for path in paths:
        if path.suffix == ".json" and (summary := _read_summary(path)) is not None:
            summaries.append(summary)
    return summaries
