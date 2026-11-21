#!/usr/bin/env python3
"""Dispatch a bounded task from a lead agent session to a Codex, Claude, or Grok worker.

Each task runs headless in its own git worktree outside the checkout, so a
worker never touches the lead's working tree. A ``read`` task runs in a
detached worktree that is removed afterwards. A ``write`` task keeps its
worktree on branch ``delegate/NAME`` for the lead to review, collect and
merge. Workers do not commit, push or install packages. The lead owns Git.

This is maintainer tooling. It launches the real provider CLIs with the
maintainer's existing logins. CI uses synthetic worker commands.
Job metadata goes to the ignored ``.firstchair/delegations/NAME/`` directory.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .accounts import account_environment, list_profiles
from .claude_json import parse_claude_json
from .codex_jsonl import CodexUsage, parse_codex_jsonl
from .provider import ProviderError
from .quota import read_quota
from .redaction import redact_record
from .rotation import Policy, Rotation, account_key, ledger_path
from .sessions import SESSION_ENV, require_owner, resolve_session
from .versions import read_version

DESCRIPTION = "Dispatch a bounded task to a Codex, Claude Code, or Grok worker in a git worktree."
PROVIDERS = ("codex", "claude", "grok")
LEAD_NAMES = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok Build"}
# auto never selects the lead. self is the explicit request for the lead's own provider.
WORKER_ORDER = {
    # Automatic write selection never picks Grok. See GROK_WRITE_REFUSAL.
    "claude": {"read": ("grok", "codex"), "write": ("codex",)},
    "codex": {"read": ("grok", "claude"), "write": ("claude",)},
    "grok": {"read": ("codex", "claude"), "write": ("codex", "claude")},
}
MODES = ("read", "write")
GROK_WRITE_REFUSAL = (
    "a Grok write worker runs with permission prompts bypassed, terminal commands and "
    "network access; pass --allow-grok-write to accept that for this run"
)
BRANCH_PREFIX = "delegate/"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
DEFAULT_TIMEOUT = {"read": 600, "write": 1800}
REPLY_LIMIT = 20_000
USAGE_LOG_LIMIT = 2 * 1024 * 1024
# Delegation logs and run records never travel into a worker's worktree.
STATE_EXCLUDES = (":(exclude).firstchair", ":(exclude).triadmux")

PREAMBLE = """\
You are a {provider} worker dispatched by the lead {lead} session for the
Firstchair repository. You run headless, so nobody can answer questions mid-task.

Rules for this task:
- Read AGENTS.md first and follow it, plus any instructions below.
- Mode: {mode}. {mode_rule}
- Do not run git commit, push, merge, rebase, reset, checkout or branch commands.
  Do not install packages. Do not use the network except for your own model API.
- Never read or copy .firstchair/, .triadmux/, credentials, transcripts or account data.
  Use invented fixtures only.
- Stay inside the scope below. If it is ambiguous, pick the safest reading and say so.

End your reply with these sections, in this order:
## Summary
## Files changed
## Verification
(exact commands you ran and their results; say if you skipped any)
## Open issues

Task:
"""

MODE_RULES = {
    "read": "Do not create, edit or delete files. Investigate and report only.",
    "write": (
        "Edit files in this worktree to complete the task and run the project tests "
        "(PYTHONPATH=src python -m unittest discover -s tests -v) before you finish."
    ),
}


class DelegateError(Exception):
    """A usage or environment problem reported without a traceback."""


def session_id(args: argparse.Namespace, *, create: bool = False) -> str | None:
    """The lead session's ID from --session, else FIRSTCHAIR_SESSION."""
    return resolve_session(getattr(args, "session", None), create=create)


def job_session(repo: Path, name: str) -> object:
    """The session recorded for a delegate job, or None for an older job."""
    path = delegation_dir(repo, name) / "session"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return ""


@dataclass(frozen=True)
class Job:
    provider: str
    mode: str
    name: str
    repo: Path
    worktree: Path
    log_dir: Path
    timeout: float
    model: str | None = None
    effort: str | None = None
    lead: str = "claude"

    @property
    def branch(self) -> str | None:
        return BRANCH_PREFIX + self.name if self.mode == "write" else None


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args), capture_output=True, text=True, check=False
    )
    if check and result.returncode:
        raise DelegateError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def repo_root(start: Path) -> Path:
    try:
        return Path(git(start, "rev-parse", "--show-toplevel").strip())
    except DelegateError:
        raise DelegateError(f"{start} is not inside a git repository") from None


def worktree_root(repo: Path, legacy: bool = False) -> Path:
    # TRIADMUX_WORKTREE_ROOT and ~/.cache/triadmux are the former names. New
    # worktrees use the Firstchair names; old ones stay findable for one release.
    variable = "TRIADMUX_WORKTREE_ROOT" if legacy else "FIRSTCHAIR_WORKTREE_ROOT"
    base = os.environ.get(variable) or (None if legacy else os.environ.get("TRIADMUX_WORKTREE_ROOT"))
    if base:
        return Path(base).expanduser().resolve() / repo.name
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    product = "triadmux" if legacy else "firstchair"
    return Path(cache).expanduser().resolve() / product / "worktrees" / repo.name


def delegation_dir(repo: Path, name: str) -> Path:
    """Logs for NAME. Delegations started before the rename stay under .triadmux/."""
    legacy = repo / ".triadmux" / "delegations" / name
    if legacy.is_dir() and not (repo / ".firstchair" / "delegations" / name).exists():
        return legacy
    return repo / ".firstchair" / "delegations" / name


def default_name(provider: str) -> str:
    return f"{provider}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def check_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise DelegateError("name must be 1-63 characters of a-z, 0-9, '.', '_' or '-'")
    return name


def build_prompt(job: Job, task: str) -> str:
    preamble = PREAMBLE.format(
        provider=LEAD_NAMES[job.provider], lead=LEAD_NAMES[job.lead],
        mode=job.mode, mode_rule=MODE_RULES[job.mode],
    )
    return preamble + task.strip() + "\n"


def build_command(job: Job, prompt_file: Path, reply_file: Path) -> tuple[list[str], bool]:
    """Return the worker argv and whether the prompt goes to stdin."""
    if job.provider == "codex":
        sandbox = "workspace-write" if job.mode == "write" else "read-only"
        argv = [
            "codex", "--ask-for-approval", "never", "exec", "--ephemeral",
            "--sandbox", sandbox, "--cd", str(job.worktree), "--color", "never",
            "--json", "--output-last-message", str(reply_file),
        ]
        if job.model:
            argv += ["--model", job.model]
        if job.effort:
            argv += ["--config", f"model_reasoning_effort={json.dumps(job.effort)}"]
        return [*argv, "-"], True

    if job.provider == "claude":
        # Installed Claude Code 2.1.330 --help: --restricted removes command and
        # WebFetch tools unless --tools names them, and confines file tools to
        # the working directory. The worker cwd is its worktree. --permission-prompts
        # none denies anything that would still wait for a person. Reply text is
        # accepted only by the versioned parser, not by a zero exit code.
        editing = job.mode == "write"
        tools = "Read,Grep,Glob,Edit,Write" if editing else "Read,Grep,Glob"
        argv = [
            "claude", "--restricted", "-p",
            "--output-format", "json", "--no-session-persistence",
            "--setting-sources", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--tools", tools,
            "--permission-mode", "acceptEdits" if editing else "dontAsk",
            "--permission-prompts", "none",
        ]
        if job.model:
            argv += ["--model", job.model]
        if job.effort:
            argv += ["--effort", job.effort]
        return argv, True

    if job.provider != "grok":
        raise DelegateError(f"unknown worker {job.provider}")
    # Grok's read-only sandbox needs bubblewrap, which cannot start on every host.
    # The Landlock workspace profile limits writes to the disposable worktree,
    # temp dirs and ~/.grok. Read tasks also lose the edit tools.
    argv = [
        "grok", "--no-auto-update", "--prompt-file", str(prompt_file),
        "--cwd", str(job.worktree), "--sandbox", "workspace",
        "--permission-mode", "bypassPermissions", "--output-format", "json",
        "--no-subagents",
    ]
    if job.mode == "read":
        argv += ["--disallowed-tools", "write,search_replace"]
    if job.model:
        argv += ["--model", job.model]
    if job.effort:
        argv += ["--reasoning-effort", job.effort]
    return argv, False


def create_worktree(job: Job) -> None:
    if job.worktree.exists():
        raise DelegateError(f"worktree {job.worktree} already exists; choose another --name")
    job.worktree.parent.mkdir(parents=True, exist_ok=True)
    if job.branch:
        if git(job.repo, "branch", "--list", job.branch).strip():
            raise DelegateError(f"branch {job.branch} already exists; choose another --name")
        git(job.repo, "worktree", "add", "-q", "-b", job.branch, str(job.worktree), "HEAD")
    else:
        git(job.repo, "worktree", "add", "-q", "--detach", str(job.worktree), "HEAD")


def carry_changes(repo: Path, worktree: Path) -> list[str]:
    """Copy the lead's uncommitted tracked diff and untracked files into a new worktree."""
    diff = subprocess.run(
        ("git", "-C", str(repo), "diff", "HEAD", "--binary"), capture_output=True, check=True
    ).stdout
    if diff:
        applied = subprocess.run(
            ("git", "-C", str(worktree), "apply", "--whitespace=nowarn"),
            input=diff, capture_output=True, check=False,
        )
        if applied.returncode:
            raise DelegateError("could not apply uncommitted changes to the worktree")
    untracked = git(
        repo, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", *STATE_EXCLUDES
    ).split("\0")
    for relative in filter(None, untracked):
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy a symlink as a link so its target's contents never leave the checkout.
        shutil.copy2(repo / relative, target, follow_symlinks=False)
    status = git(repo, "status", "--porcelain", "--", ".", *STATE_EXCLUDES)
    return [line[3:] for line in status.splitlines()]


def remove_worktree(repo: Path, worktree: Path) -> None:
    if worktree.exists():
        git(repo, "worktree", "remove", "--force", str(worktree))
    git(repo, "worktree", "prune", check=False)


class Terminated(Exception):
    """SIGTERM reached the controller while a worker was running."""


def _on_sigterm(signum: int, frame: object) -> None:
    raise Terminated


def _wait(process: subprocess.Popen[bytes], timeout: float, stop: threading.Event | None) -> int:
    """Wait for the worker. A set ``stop`` event ends the wait with Terminated."""
    if stop is None:
        return process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        if stop.is_set():
            raise Terminated
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=min(remaining, 0.1))
        except subprocess.TimeoutExpired:
            continue


def run_worker(
    argv: list[str], stdin_file: Path | None, job: Job, runtime_dir: Path, env: dict[str, str] | None,
    stop: threading.Event | None = None,
) -> tuple[int | None, float]:
    """Run one worker in its own process group. Return (exit code or None on timeout, seconds).

    Signal handlers only run on the main thread. A caller that runs workers on
    other threads passes ``stop`` and sets it to end them.
    """
    if stop is not None and stop.is_set():
        raise Terminated
    started = time.monotonic()
    # A file on stdin cannot block the controller the way a full pipe can, so the
    # timeout covers the whole run, and an early worker exit cannot raise EPIPE.
    with (
        open(stdin_file if stdin_file else os.devnull, "rb") as stdin,
        open(runtime_dir / "stdout.log", "wb") as stdout,
        open(runtime_dir / "stderr.log", "wb") as stderr,
    ):
        try:
            process = subprocess.Popen(
                argv, cwd=job.worktree, stdin=stdin, stdout=stdout, stderr=stderr,
                start_new_session=os.name == "posix", env=env,
                umask=0o077 if env is not None else -1,
            )
        except OSError as error:
            raise DelegateError(f"could not start {argv[0]}: {error.strerror}") from None
        try:
            code: int | None = _wait(process, job.timeout, stop)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            # Also reaps grandchildren that outlive a worker that exited normally.
            _kill_group(process)
    return code, time.monotonic() - started


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def extract_reply(job: Job, reply_file: Path, runtime_dir: Path) -> str:
    if job.provider == "codex":
        return reply_file.read_text(encoding="utf-8", errors="replace") if reply_file.exists() else ""
    raw = (runtime_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
    try:
        result = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return ""


def read_claude_reply(runtime_dir: Path) -> tuple[str, CodexUsage]:
    """Accept a Claude reply only when the versioned parser accepts it."""
    try:
        version_result = subprocess.run(
            ["claude", "--version"], capture_output=True, timeout=3, check=True, text=True,
        )
        version = read_version("claude", version_result.stdout)
        output = (runtime_dir / "stdout.log").read_bytes()
        if len(output) > USAGE_LOG_LIMIT:
            raise ProviderError("malformed_response")
        parsed = parse_claude_json(output, version=version, returncode=0)
    except (OSError, ValueError, subprocess.SubprocessError, ProviderError):
        raise ProviderError("malformed_response") from None
    return parsed.text, parsed.usage


def worker_usage(job: Job, returncode: int | None, runtime_dir: Path) -> CodexUsage:
    """Report only telemetry recognized by the versioned offline parser."""
    if job.provider != "codex" or returncode != 0:
        return CodexUsage()
    try:
        version_result = subprocess.run(
            ["codex", "--version"], cwd=job.worktree, capture_output=True,
            timeout=3, check=True, text=True,
        )
        name, version = version_result.stdout.strip().split()
        if name != "codex-cli":
            return CodexUsage()
        with (runtime_dir / "stdout.log").open("rb") as stream:
            output = stream.read(USAGE_LOG_LIMIT + 1)
        if len(output) <= USAGE_LOG_LIMIT:
            return parse_codex_jsonl(output, version=version, returncode=returncode).usage
    except (OSError, ValueError, subprocess.SubprocessError, ProviderError):
        pass
    return CodexUsage()


def worktree_changes(worktree: Path) -> dict[str, str]:
    status = git(worktree, "status", "--short", "--untracked-files=all")
    stat = git(worktree, "diff", "--stat", "HEAD")
    return {"status": status.rstrip(), "diffstat": stat.rstrip()}


def select_worker(provider: str, lead: str, mode: str) -> str:
    if provider != "auto":
        if shutil.which(provider) is None:
            raise DelegateError(f"{provider} is not on PATH")
        return provider
    for candidate in WORKER_ORDER[lead][mode]:
        if shutil.which(candidate) is not None:
            return candidate
    raise DelegateError(f"no eligible {mode} worker for {LEAD_NAMES[lead]} is on PATH")


def resolve_seat(seat: str, lead: str, mode: str, *, allow_grok_write: bool = False) -> str:
    """Resolve a conductor seat. ``self`` is the lead's own provider.

    A Grok write worker is refused unless the caller opted in for this run.
    """
    if seat == "self":
        if lead not in LEAD_NAMES:
            raise DelegateError(f"unknown lead {lead}")
        if shutil.which(lead) is None:
            raise DelegateError(f"{lead} is not on PATH")
        provider = lead
    elif seat not in {*PROVIDERS, "auto"}:
        raise DelegateError(f"unknown seat {seat}")
    else:
        provider = select_worker(seat, lead, mode)
    if provider == "grok" and mode == "write" and allow_grok_write is not True:
        raise DelegateError(GROK_WRITE_REFUSAL)
    return provider


@dataclass(frozen=True)
class Account:
    provider: str
    profile: str | None


@dataclass
class Choice:
    """The account a worker runs under, and how it was picked."""

    account: Account
    rotation: Rotation | None = None
    candidates: list[dict[str, object]] | None = None


def choose_account(provider: str) -> Choice:
    """Pick the worker's account from real usage: the default sign-in or a named one."""
    try:
        named = [item["profile"] for item in list_profiles() if item["provider"] == provider]
        seats = [Account(provider, None), *(Account(provider, name) for name in named)]
        rotation = Rotation(
            ledger_path(), Policy(), reader=lambda seat: read_quota(seat.provider, seat.profile),
        )
        usable, candidates = rotation.choose(seats, seats[0])
    except ValueError as error:
        raise DelegateError(str(error)) from None
    if not usable:
        raise DelegateError(f"every {provider} account is out of usage")
    return Choice(usable[0], rotation, candidates)


def _resolve(args: argparse.Namespace) -> tuple[Path, str, dict[str, str] | None, str, float, Choice]:
    repo = repo_root(Path(args.repo or Path.cwd()))
    if args.provider in {"auto", "self"} and (args.model or args.effort):
        raise DelegateError("--model and --effort require an explicit worker")
    if args.profile is not None and args.provider in {"auto", "self"}:
        raise DelegateError("--profile requires an explicit worker")
    provider = resolve_seat(
        args.provider, args.lead, args.mode,
        allow_grok_write=getattr(args, "allow_grok_write", False),
    )
    if getattr(args, "rotate_accounts", False):
        if args.profile is not None:
            raise DelegateError("--rotate-accounts picks the account; leave out --profile")
        choice = choose_account(provider)
    else:
        choice = Choice(Account(provider, args.profile))
    profile = choice.account.profile
    env = account_environment(provider, profile) if profile is not None else None
    name = check_name(args.name or default_name(provider))
    timeout = DEFAULT_TIMEOUT[args.mode] if args.timeout is None else args.timeout
    if not math.isfinite(timeout) or timeout <= 0:
        raise DelegateError("--timeout must be a finite positive number")
    return repo, provider, env, name, timeout, choice


def dispatch(args: argparse.Namespace) -> int:
    """Run one worker for the command line: own SIGTERM and print the summary."""
    if args.dry_run:
        _, provider, _, _, timeout, choice = _resolve(args)
        print(json.dumps({
            "status": "planned",
            "lead": args.lead,
            "requested_provider": args.provider,
            "provider": provider,
            "mode": args.mode,
            "timeout_seconds": timeout,
            "model": args.model,
            "effort": args.effort,
            "carry_changes": args.with_changes,
            "account_selection": (
                "rotated" if choice.rotation is not None
                else "named" if args.profile is not None else "default"
            ),
            **({
                "account": account_key(provider, choice.account.profile),
                "account_candidates": choice.candidates,
            } if choice.rotation is not None else {}),
        }, indent=2))
        return 0
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        code, summary = launch_job(args)
    except Terminated:
        raise DelegateError("terminated by SIGTERM; worker stopped") from None
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(summary, indent=2))
    return code


def launch_job(
    args: argparse.Namespace, *,
    stop: threading.Event | None = None,
    setup_lock: threading.Lock | None = None,
) -> tuple[int, dict[str, object]]:
    """Run one worker and return its exit code and summary.

    This neither prints nor touches signal handlers, so several calls can run
    on separate threads. The caller owns SIGTERM. Terminated propagates after
    the worker's process group is killed and a disposable worktree is removed.
    ``setup_lock`` serializes the git steps before and after the worker. The
    worker process itself runs outside the lock.
    """
    repo, provider, env, name, timeout, choice = _resolve(args)
    session = session_id(args, create=True)
    task = read_task(args)
    job = Job(
        provider=provider, mode=args.mode, name=name, repo=repo,
        worktree=worktree_root(repo) / name,
        log_dir=repo / ".firstchair" / "delegations" / name,
        timeout=timeout,
        model=args.model, effort=args.effort, lead=args.lead,
    )

    def guard() -> contextlib.AbstractContextManager[object]:
        return setup_lock if setup_lock is not None else contextlib.nullcontext()

    with guard():
        if job.log_dir.exists():
            raise DelegateError(f"log directory {job.log_dir} already exists; choose another --name")
        job.log_dir.mkdir(parents=True, mode=0o700)
        (job.log_dir / "session").write_text(f"{session}\n", encoding="utf-8")
        create_worktree(job)
        head = git(job.worktree, "rev-parse", "HEAD").strip()
    keep = job.mode == "write" or args.keep
    carried: list[str] = []
    accepted = False
    try:
        with guard():
            if args.with_changes:
                carried = carry_changes(repo, job.worktree)
                if job.branch and carried:
                    # Keep the lead's carried work in its own commit so the worker's
                    # edits stay a separate, cherry-pickable commit after collect.
                    git(job.worktree, "add", "--all")
                    git(job.worktree, "commit", "-q", "--no-verify", "-m",
                        f"Carry lead's uncommitted changes into delegate {name}")
            baseline = worktree_changes(job.worktree)["status"]
        with tempfile.TemporaryDirectory(prefix="firstchair-delegate-") as scratch:
            runtime_dir = Path(scratch)
            prompt_file = runtime_dir / "prompt.md"
            prompt_file.write_text(build_prompt(job, task), encoding="utf-8")
            reply_file = runtime_dir / "reply.md"
            argv, use_stdin = build_command(job, prompt_file, reply_file)
            code, elapsed = run_worker(
                argv, prompt_file if use_stdin else None, job, runtime_dir, env, stop,
            )
            usage = worker_usage(job, code, runtime_dir)
            reply = ""
            accepted = code == 0
            if code == 0 and provider == "claude":
                try:
                    reply, usage = read_claude_reply(runtime_dir)
                except ProviderError:
                    accepted = False
            elif code == 0:
                reply = extract_reply(job, reply_file, runtime_dir)
        with guard():
            changes = worktree_changes(job.worktree)
    finally:
        if not keep:
            with guard():
                remove_worktree(repo, job.worktree)

    status = "timeout" if code is None else ("completed" if accepted else "failed")
    summary = {
        "name": name,
        "session": session,
        "provider": provider,
        "lead": job.lead,
        "requested_provider": args.provider,
        "mode": job.mode,
        "status": status,
        "exit_code": code,
        "elapsed_seconds": round(elapsed, 1),
        "usage": asdict(usage),
        "base_commit": head,
        "worktree": str(job.worktree) if keep else None,
        "branch": job.branch,
        "carried_changes": carried,
        "logs": str(job.log_dir),
        "changes": changes,
        "reply": reply if len(reply) <= REPLY_LIMIT else reply[:REPLY_LIMIT] + "\n[truncated]",
    }
    if choice.rotation is not None:
        summary["account"] = account_key(provider, choice.account.profile)
        summary["account_candidates"] = choice.candidates
        if status == "completed":
            # Count the work against the account. A failed worker's cause is not known.
            choice.rotation.observe(choice.account, usage, None)
    if job.mode == "read" and changes["status"] != baseline:
        summary["warning"] = "read task changed files in its disposable worktree; changes discarded"
    redact_record(summary)
    metadata = {key: summary[key] for key in ("name", "session", "provider", "lead", "mode", "status", "usage")}
    (job.log_dir / "summary.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return (0 if status == "completed" else (124 if code is None else 1)), summary


def read_task(args: argparse.Namespace) -> str:
    if args.prompt_file:
        task = Path(args.prompt_file).read_text(encoding="utf-8")
    elif args.task == "-" or args.task is None:
        if sys.stdin.isatty():
            raise DelegateError("pass a task, --prompt-file, or pipe the task on stdin")
        task = sys.stdin.read()
    else:
        task = args.task
    if not task.strip():
        raise DelegateError("task must contain text")
    return task


def delegate_worktrees(repo: Path) -> list[dict[str, str]]:
    roots = {worktree_root(repo), worktree_root(repo, legacy=True)}
    entries, current = [], {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    found = []
    for entry in entries:
        path = Path(entry.get("worktree", ""))
        if path.parent in roots:
            branch = entry.get("branch", "").removeprefix("refs/heads/")
            found.append({"name": path.name, "worktree": str(path), "branch": branch or None})
    return found


def list_jobs(args: argparse.Namespace) -> int:
    repo = repo_root(Path(args.repo or Path.cwd()))
    jobs = []
    for job in delegate_worktrees(repo):
        changes = worktree_changes(Path(job["worktree"]))
        owner = job_session(repo, job["name"])
        jobs.append({
            **job, "dirty": bool(changes["status"]),
            "session": owner if isinstance(owner, str) and owner else None,
        })
    branches = [
        line.strip() for line in git(repo, "branch", "--list", BRANCH_PREFIX + "*",
                                     "--format=%(refname:short)").splitlines()
    ]
    print(json.dumps({"worktrees": jobs, "branches": branches}, indent=2))
    return 0


def find_job(repo: Path, name: str) -> dict[str, str]:
    """Find a worktree this script created: delegate branch or detached, with its logs."""
    check_name(name)
    for job in delegate_worktrees(repo):
        if job["name"] != name:
            continue
        owned_branch = job["branch"] in (None, BRANCH_PREFIX + name)
        if not owned_branch or not delegation_dir(repo, name).is_dir():
            raise DelegateError(f"worktree {name} was not created by delegate.py; leaving it alone")
        return job
    raise DelegateError(f"no delegate worktree named {name}")


def collect(args: argparse.Namespace) -> int:
    """Commit a write worker's changes on its branch so the lead can review and merge them."""
    repo = repo_root(Path(args.repo or Path.cwd()))
    job = find_job(repo, args.name)
    require_owner(job_session(repo, args.name), session_id(args), f"delegate job {args.name}")
    if not job["branch"]:
        raise DelegateError(f"{args.name} has no branch; only write tasks can be collected")
    worktree = Path(job["worktree"])
    git(worktree, "add", "--all")
    if not git(worktree, "status", "--porcelain").strip():
        print(json.dumps({"name": args.name, "branch": job["branch"], "committed": False}))
        return 0
    summary_file = delegation_dir(repo, args.name) / "summary.json"
    provider = "worker"
    if summary_file.exists():
        provider = json.loads(summary_file.read_text()).get("provider", provider)
    message = args.message or f"Collect {provider} delegate {args.name}"
    git(worktree, "commit", "-q", "-m", message)
    commit = git(worktree, "rev-parse", "HEAD").strip()
    stat = git(repo, "diff", "--stat", f"HEAD...{job['branch']}", check=False).rstrip()
    print(json.dumps({
        "name": args.name, "branch": job["branch"], "committed": True,
        "commit": commit, "diffstat_vs_head": stat,
    }, indent=2))
    return 0


def finish(args: argparse.Namespace) -> int:
    """Remove a delegate worktree; with --delete-branch also drop its branch."""
    repo = repo_root(Path(args.repo or Path.cwd()))
    job = find_job(repo, args.name)
    require_owner(job_session(repo, args.name), session_id(args), f"delegate job {args.name}")
    worktree = Path(job["worktree"])
    if worktree_changes(worktree)["status"] and not args.force:
        raise DelegateError(f"{args.name} has uncommitted changes; collect it or pass --force")
    remove_worktree(repo, worktree)
    deleted = False
    if args.delete_branch and job["branch"]:
        git(repo, "branch", "-D" if args.force else "-d", job["branch"])
        deleted = True
    print(json.dumps({"name": args.name, "removed": True, "branch_deleted": deleted}))
    return 0


def configure_parser(root: argparse.ArgumentParser) -> None:
    root.add_argument("--repo", help="Repository path (default: current checkout)")
    root.add_argument(
        "--session", default=argparse.SUPPRESS,
        help=f"This lead session's ID (default: ${SESSION_ENV}; run makes one when unset)",
    )
    commands = root.add_subparsers(dest="delegate_command", required=True)

    run = commands.add_parser("run", help="Dispatch one task to a worker")
    run.add_argument("provider", choices=(*PROVIDERS, "auto", "self"))
    run.add_argument("--lead", choices=LEAD_NAMES, default="claude",
                     help="Agent running the lead session (default: claude)")
    run.add_argument("task", nargs="?", help="Task text, or '-' to read stdin")
    run.add_argument("--mode", choices=MODES, default="read")
    run.add_argument("--name", help="Job name; also the worktree and branch suffix")
    run.add_argument("--prompt-file", help="Read the task from this file")
    run.add_argument("--timeout", type=float, help="Seconds before the worker is killed")
    run.add_argument("--profile", help="Use a named login for the explicit worker")
    run.add_argument(
        "--rotate-accounts", action="store_true",
        help="Pick the worker's account from real usage: its default sign-in or a named one",
    )
    run.add_argument("--model", help="Provider model override")
    run.add_argument("--effort", help="Provider reasoning effort override")
    run.add_argument("--dry-run", action="store_true", help="Show the selection without launching a worker")
    run.add_argument("--keep", action="store_true", help="Keep a read task's worktree")
    run.add_argument(
        "--allow-grok-write", action="store_true",
        help="Accept a Grok write worker, which has terminal commands and network access",
    )
    run.add_argument(
        "--with-changes", action="store_true",
        help="Copy the checkout's uncommitted and untracked files into the worktree",
    )
    run.set_defaults(handler=dispatch)

    listing = commands.add_parser("list", help="List delegate worktrees and branches")
    listing.set_defaults(handler=list_jobs)

    gather = commands.add_parser("collect", help="Commit a write task's changes on its branch")
    gather.add_argument("name")
    gather.add_argument("--message", "-m")
    gather.set_defaults(handler=collect)

    done = commands.add_parser("finish", help="Remove a delegate worktree")
    done.add_argument("name")
    done.add_argument("--delete-branch", action="store_true")
    done.add_argument("--force", action="store_true", help="Discard uncommitted or unmerged work")
    done.set_defaults(handler=finish)


def execute(args: argparse.Namespace) -> int:
    try:
        return args.handler(args)
    except (DelegateError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(description=DESCRIPTION)
    configure_parser(root)
    return execute(root.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
