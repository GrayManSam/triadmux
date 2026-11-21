"""Run a deterministic three-stage workflow and persist its progress."""

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .fallback import is_transient
from .handoff import WorkerReview, read_review, review_assignment, review_text
from .provider import ProviderError
from .redaction import redact_record
from .sessions import require_owner

STAGES = ("plan", "review", "synthesize")


def record_path(state_dir: Path, run_id: str) -> Path:
    parsed = UUID(run_id)
    if str(parsed) != run_id:
        raise ValueError("run ID must be a canonical UUID")
    return state_dir / f"{parsed}.json"


def save(path: Path, record: dict[str, object]) -> None:
    redact_record(record)
    stream = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    )
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(record, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _run_lock(path: Path, *, create: bool) -> Iterator[None]:
    if os.name != "posix":
        if not create:
            raise ValueError("run recovery requires POSIX file locking")
        yield
        return

    import fcntl

    # Keep the lock inode stable across atomic replacements of the JSON record.
    lock_path = path.with_suffix(".lock")
    flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0)
    try:
        lock = os.open(lock_path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        raise ValueError("run has no recovery lock") from None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("run is locked by another controller") from None
        yield
    finally:
        os.close(lock)


def recover_workflow(
    state_dir: Path, run_id: str, *, session: str | None = None,
) -> dict[str, object]:
    """Mark an abandoned run interrupted without replaying its work."""
    path = record_path(state_dir, run_id)
    with _run_lock(path, create=False):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1
            or record.get("run_id") != run_id
        ):
            raise ValueError("invalid or unsupported run record")
        require_owner(record.get("session"), session, f"run {run_id}")
        if record.get("status") != "running":
            raise ValueError("only running records can be recovered")
        record.update(status="interrupted", error="controller_crash")
        save(path, record)
        return record


def run_workflow[Review: (str, WorkerReview)](
    task: str, state_dir: Path,
    plan: Callable[[str], str], review: Callable[[str], Review], synthesize: Callable[[Review], str],
    *, details: Callable[[], dict[str, object]] | None = None,
    provider: str = "demo", prelude: dict[str, object] | None = None,
    session: str | None = None,
) -> dict[str, object]:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_id = str(uuid4())
    path = record_path(state_dir, run_id)
    record = _new_record(run_id, task, provider)
    if session is not None:
        record["session"] = session
    if prelude:
        record.update(prelude)
    with _run_lock(path, create=True):
        save(path, record)
        return _advance(path, record, task, plan, review, synthesize, details)


def continue_workflow[Review: (str, WorkerReview)](
    state_dir: Path, run_id: str,
    plan: Callable[[str], str], review: Callable[[str], Review], synthesize: Callable[[Review], str],
    *, details: Callable[[], dict[str, object]] | None = None,
    session: str | None = None,
) -> dict[str, object]:
    """Finish an interrupted, abandoned, or usage-failed run from its saved stages."""
    path = record_path(state_dir, run_id)
    with _run_lock(path, create=False):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1
            or record.get("run_id") != run_id
        ):
            raise ValueError("invalid or unsupported run record")
        require_owner(record.get("session"), session, f"run {run_id}")
        status = record.get("status")
        if status == "completed":
            raise ValueError("a completed run cannot be resumed")
        if status == "failed" and not is_transient(str(record.get("error"))):
            raise ValueError("only an interrupted run or a usage failure can be resumed")
        if status not in {"running", "interrupted", "failed"}:
            raise ValueError("only an interrupted run or a usage failure can be resumed")
        task = record.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("run has no saved task")
        outputs = record.get("stage_outputs")
        if not isinstance(outputs, dict):
            outputs = _outputs_from_stages(record)
            record["stage_outputs"] = outputs
        _require_saved_outputs(outputs)
        record["status"] = "running"
        record.pop("error", None)
        save(path, record)
        return _advance(path, record, task, plan, review, synthesize, details)


def _new_record(run_id: str, task: str, provider: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "task": task,
        "provider": provider,
        "status": "running",
        "stage": "plan",
        "stage_outputs": {},
        "usage": {
            "provenance": "unknown",
            "input_tokens": None,
            "output_tokens": None,
        },
    }


def _require_saved_outputs(outputs: dict[str, object]) -> None:
    """Reject a resume payload before the saved status is replaced."""
    for name, saved in outputs.items():
        if name not in STAGES:
            raise ValueError("invalid stage output")
        if isinstance(saved, dict):
            if name != "review":
                raise ValueError("invalid stage output")
            read_review(saved)
        elif not isinstance(saved, str):
            raise ValueError("invalid stage output")


def _outputs_from_stages(record: dict[str, object]) -> dict[str, object]:
    stages = record.get("stages")
    outputs: dict[str, object] = {}
    if not isinstance(stages, dict):
        return outputs
    for name in STAGES:
        stage = stages.get(name)
        if isinstance(stage, dict) and isinstance(stage.get("text"), str):
            outputs[name] = stage["text"]
    return outputs


def _advance[Review: (str, WorkerReview)](
    path: Path, record: dict[str, object], task: str,
    plan: Callable[[str], str], review: Callable[[str], Review], synthesize: Callable[[Review], str],
    details: Callable[[], dict[str, object]] | None,
) -> dict[str, object]:
    outputs = record.get("stage_outputs")
    if not isinstance(outputs, dict):
        raise ValueError("invalid stage outputs")

    def checkpoint(**fields: object) -> None:
        # Stage outputs and usage are recorded even when a later stage fails.
        # A details() failure must not leave the record running with the error cleared.
        try:
            if details is not None:
                extra = details()
                if isinstance(extra, dict):
                    record.update(extra)
        except Exception:
            pass
        record.update(fields)
        save(path, record)

    value: str | WorkerReview = task
    worker_review = None
    for name, stage in (("plan", plan), ("review", review), ("synthesize", synthesize)):
        if name in outputs:
            saved = outputs[name]
            if isinstance(saved, dict):
                if name != "review":
                    raise ValueError("invalid stage output")
                review_output = read_review(saved)
                worker_review = review_output
                value = review_output
            elif isinstance(saved, str):
                value = saved
            else:
                raise ValueError("invalid stage output")
            continue
        record["stage"] = name
        save(path, record)
        try:
            value = stage(value)
        except ProviderError as error:
            checkpoint(status="failed", error=str(error))
            return record
        except Exception:
            checkpoint(status="failed", error="stage_failed")
            return record
        except KeyboardInterrupt:
            checkpoint(status="interrupted")
            return record
        outputs[name] = value
        if name == "review" and isinstance(value, dict):
            worker_review = value
        if details is not None:
            checkpoint()
        else:
            save(path, record)
    if worker_review is not None:
        record["worker_review"] = worker_review
    checkpoint(status="completed", stage="synthesize", result=value)
    return record


def demo_plan(task: str) -> str:
    return f"Identify the acceptance criteria for: {task}"


def demo_review(assignment: str) -> str:
    return review_text(review_assignment(assignment))


def demo_synthesize(review: str) -> str:
    return f"Demo handoff\n{review}"
