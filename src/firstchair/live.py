"""Read-only live adapters for Codex CLI, Claude Code and Grok Build.

Each stage launches one bounded, non-interactive provider process. The
controller chooses the executable, flags, working directory, environment and
limits. Provider output is parsed fail-closed by a version-gated parser.
"""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic
from typing import BinaryIO
from typing import Any

from .accounts import CREDENTIAL_OVERRIDES, HOME_VARIABLE, account_environment, profile_home
from .fallback import is_transient
from .rotation import Rotation, account_key
from .claude_json import parse_claude_json
from .codex_jsonl import parse_codex_jsonl
from .grok_jsonl import parse_grok_jsonl
from .provider import ProviderError, _subprocess_environment, run_bounded
from .redaction import _is_secret_name
from .usage import Reply, Usage
from .versions import check_version, read_version

PROVIDERS = ("codex", "claude", "grok")
MAX_PROMPT_BYTES = 256 * 1024
# Names a signed-in CLI needs to find its own login, config and caches.
_LIVE_ENV_NAMES = frozenset({
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
})
_PARSERS: dict[str, Callable[..., Reply]] = {
    "codex": parse_codex_jsonl,
    "claude": parse_claude_json,
    "grok": parse_grok_jsonl,
}


@dataclass(frozen=True)
class LiveLimits:
    timeout: float = 600.0
    output_bytes: int = 8 * 1024 * 1024
    input_bytes: int = MAX_PROMPT_BYTES

    def __post_init__(self) -> None:
        if not 0 < self.timeout <= 3600:
            raise ValueError("live stage timeout must be greater than 0 and at most 3600 seconds")
        if type(self.output_bytes) is not int or not 1024 <= self.output_bytes <= 64 * 1024 * 1024:
            raise ValueError("live output limit must be between 1024 and 67108864 bytes")
        if type(self.input_bytes) is not int or not 1 <= self.input_bytes <= MAX_PROMPT_BYTES:
            raise ValueError(f"live prompt limit must be between 1 and {MAX_PROMPT_BYTES} bytes")


def live_environment(provider: str, profile: str | None = None) -> dict[str, str]:
    """Allowlisted environment plus what the CLI needs to find its own login.

    API keys never pass through, so each CLI uses its native sign-in.
    """
    source = account_environment(provider, profile) if profile is not None else dict(os.environ)
    # Test-prefixed names pass for fake CLIs, but never a secret-looking one.
    environment = {
        name: value for name, value in _subprocess_environment().items()
        if not _is_secret_name(name)
    }
    for name in (*_LIVE_ENV_NAMES, HOME_VARIABLE[provider]):
        if name in source and name not in CREDENTIAL_OVERRIDES:
            environment[name] = source[name]
    environment["NO_COLOR"] = "1"
    return environment


def find_executable(provider: str, env: dict[str, str]) -> str:
    """Resolve the CLI once to an absolute path, so probe and stages run the same file."""
    executable = shutil.which(provider, path=env.get("PATH"))
    if executable is None:
        raise ProviderError("provider_unavailable")
    return os.path.abspath(executable)


def provider_version(provider: str, env: dict[str, str] | None = None, executable: str | None = None) -> str:
    """Run `PROVIDER --version` with bounded output and deadline, and return X.Y.Z."""
    env = env if env is not None else live_environment(provider)
    executable = executable or find_executable(provider, env)
    argv = (executable, "--version") if provider != "grok" else (executable, "--no-auto-update", "--version")
    try:
        with TemporaryDirectory(prefix="firstchair-version-") as cwd:
            returncode, output = asyncio.run(_bounded(
                argv, subprocess.DEVNULL, 64 * 1024, cwd, env, 20, "provider_unavailable",
            ))
    except OSError:
        raise ProviderError("provider_unavailable") from None
    except ProviderError:
        raise ProviderError("provider_unavailable") from None
    if returncode:
        raise ProviderError("provider_unavailable")
    return read_version(provider, output.decode("utf-8", errors="replace"))


def readiness(provider: str) -> dict[str, object]:
    """Report executable, version and contract support without running a model."""
    executable = shutil.which(provider)
    report: dict[str, object] = {"executable": executable, "version": None, "contract": "missing"}
    if executable is None:
        return report
    try:
        version = provider_version(provider)
        report["version"] = version
        report["contract"] = check_version(provider, version)
    except ProviderError as error:
        report["contract"] = str(error)
    return report


def build_command(provider: str, workspace: Path | None, prompt_file: Path) -> tuple[tuple[str, ...], bool]:
    """Return a read-only argv and whether the prompt goes to stdin."""
    if provider == "codex":
        argv = (
            "codex", "--ask-for-approval", "never", "exec", "--ephemeral",
            "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never",
            # Drop user-configured MCP servers so tools cannot reach outside the sandbox.
            "--config", "mcp_servers={}", "--json",
        )
        return (*argv, "-"), True
    if provider == "claude":
        tools = "Read,Grep,Glob" if workspace is not None else ""
        argv = (
            "claude", "-p", "--output-format", "json", "--no-session-persistence",
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", tools, "--permission-mode", "dontAsk",
        )
        return argv, True
    if provider == "grok":
        # Grok's read-only sandbox needs bubblewrap. Landlock `workspace` plus
        # deny-by-default permissions and an explicit read-only tool list
        # keeps writes and MCP calls out of reach.
        tools = "read_file,list_dir,grep" if workspace is not None else ""
        argv = (
            "grok", "--no-auto-update", "--prompt-file", str(prompt_file),
            "--output-format", "streaming-json", "--no-subagents", "--disable-web-search",
            "--sandbox", "workspace", "--permission-mode", "dontAsk",
            "--tools", tools, "--deny", "mcp__*", "--deny", "Edit", "--deny", "Write",
            "--deny", "Bash", "--deny", "run_terminal_command", "--deny", "WebFetch",
        )
        return argv, False
    raise ValueError(f"unknown live provider {provider}")


@dataclass
class LiveProvider:
    """Run one read-only provider turn per call under shared limits."""

    provider: str
    limits: LiveLimits = field(default_factory=LiveLimits)
    workspace: Path | None = None
    profile: str | None = None
    deadline: float | None = None
    version: str | None = None
    executable: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown live provider {self.provider}")
        if self.profile is not None:
            profile_home(self.provider, self.profile)  # Reject unknown or unsafe profiles early.
        if self.workspace is not None:
            self.workspace = self.workspace.resolve()
            if not self.workspace.is_dir():
                raise ValueError("workspace must be an existing directory")

    def prepare(self) -> str:
        """Check the installed version once before any stage runs."""
        if self.version is None:
            env = live_environment(self.provider, self.profile)
            executable = find_executable(self.provider, env)
            version = provider_version(self.provider, env, executable)
            check_version(self.provider, version)
            self.executable, self.version = executable, version
        return self.version

    def ask(self, prompt: str) -> Reply:
        version = self.prepare()
        payload = prompt.encode("utf-8")
        if len(payload) > self.limits.input_bytes:
            raise ProviderError("input_limit")
        timeout = self.limits.timeout
        timeout_error = "provider_timeout"
        if self.deadline is not None:
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise ProviderError("budget_exhausted")
            if remaining <= timeout:
                timeout, timeout_error = remaining, "budget_exhausted"
        env = live_environment(self.provider, self.profile)
        try:
            with TemporaryDirectory(prefix="firstchair-live-") as scratch:
                prompt_file = Path(scratch) / "prompt.txt"
                prompt_file.write_bytes(payload)
                argv, use_stdin = build_command(self.provider, self.workspace, prompt_file)
                argv = (self.executable or find_executable(self.provider, env), *argv[1:])
                cwd = str(self.workspace) if self.workspace is not None else scratch
                with TemporaryFile() as request:
                    if use_stdin:
                        request.write(payload)
                        request.seek(0)
                    returncode, output = asyncio.run(
                        _bounded(argv, request, self.limits.output_bytes, cwd, env, timeout, timeout_error)
                    )
        except OSError:
            raise ProviderError("provider_unavailable") from None
        return _PARSERS[self.provider](output, version=version, returncode=returncode)


async def _bounded(
    argv: tuple[str, ...], request: BinaryIO | int, output_bytes: int, cwd: str,
    env: dict[str, str], timeout: float, timeout_error: str,
) -> tuple[int, bytes]:
    try:
        async with asyncio.timeout(timeout):
            return await run_bounded(argv, request, output_bytes, cwd=cwd, env=env)
    except TimeoutError:
        raise ProviderError(timeout_error) from None


PLAN_PROMPT = """You are the lead engineer in a multi-model review. Do not modify files.
Write a concise, concrete plan for the task below: the approach, the steps,
the files or areas likely involved, and how to verify the result.

Task:
{task}
"""

REVIEW_PROMPT = """You are an independent reviewer. Do not modify files.
Critically review the lead's plan for the task below. List concrete problems,
missing steps, risks and a better alternative where one exists. Be specific
and brief. If the plan is sound, say so and name the single most important
verification step.

Task:
{task}

Lead's plan:
{plan}
"""

SYNTHESIS_PROMPT = """You are the lead engineer. Do not modify files.
Produce the final answer for the task below. Incorporate the reviewer's
valid points, reject invalid ones with a short reason, and end with a
numbered list of next actions.

Task:
{task}

Your plan:
{plan}

Reviewer's feedback:
{review}
"""


@dataclass
class LiveSession:
    """Lead plans and synthesizes; the reviewer critiques the plan.

    A seat that is out of usage, unavailable, or timed out is skipped. The
    next named seat finishes that stage and the ones that follow. One
    remaining seat can do the whole run. A seat is one provider under one
    sign-in, so a second account of the same provider that the user named
    is a separate seat and can take over.
    """

    lead: LiveProvider
    reviewer: LiveProvider
    extras: tuple[LiveProvider, ...] = ()
    task: str = ""
    plan_text: str = ""
    stages: dict[str, dict[str, object]] = field(default_factory=dict)
    attempted: int = 0
    exhausted: list[dict[str, object]] = field(default_factory=list)
    rotation: Rotation | None = None
    selections: list[dict[str, object]] = field(default_factory=list)

    def seats(self) -> list[LiveProvider]:
        chosen: list[LiveProvider] = []
        seen: set[tuple[str, str | None]] = set()
        for seat in (self.lead, self.reviewer, *self.extras):
            key = (seat.provider, seat.profile)
            if key not in seen:
                seen.add(key)
                chosen.append(seat)
        return chosen

    def restore(self, record: dict[str, object]) -> None:
        """Reload finished stages so a stopped run does not repeat them."""
        task = record.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("run has no saved task")
        self.task = task
        self.stages = _saved_stages(record.get("stages"))
        plan = self.stages.get("plan")
        if isinstance(plan, dict) and isinstance(plan.get("text"), str):
            self.plan_text = plan["text"]
        else:
            outputs = record.get("stage_outputs")
            saved = outputs.get("plan") if isinstance(outputs, dict) else None
            if isinstance(saved, str):
                self.plan_text = saved
            elif plan is not None:
                raise ValueError("invalid plan stage")
        self.exhausted = _saved_exhausted(record.get("exhausted", []))
        saved = record.get("selection", [])
        if not isinstance(saved, list) or not all(isinstance(item, dict) for item in saved):
            raise ValueError("invalid selection record")
        self.selections = list(saved)
        if "attempted" not in record:
            self.attempted = 0
        else:
            attempted = record["attempted"]
            if type(attempted) is not int or attempted < 0:
                raise ValueError("invalid attempt count")
            self.attempted = attempted

    def spent(self) -> set[tuple[object, object]]:
        """Seats retired for this run, as (provider, profile)."""
        return {(item.get("provider"), item.get("profile")) for item in self.exhausted}

    def roster(self) -> list[dict[str, object]]:
        """Each seat with the roles it holds, so a resume keeps the same roles."""
        entries = []
        for seat in self.seats():
            key = (seat.provider, seat.profile)
            roles = [
                role for role, holder in (("lead", self.lead), ("reviewer", self.reviewer))
                if (holder.provider, holder.profile) == key
            ]
            entries.append({"provider": seat.provider, "profile": seat.profile, "roles": roles})
        return entries

    def _available(self, preferred: LiveProvider) -> list[LiveProvider]:
        spent = self.spent()
        available = [seat for seat in self.seats() if (seat.provider, seat.profile) not in spent]
        key = (preferred.provider, preferred.profile)
        return [seat for seat in available if (seat.provider, seat.profile) == key] + [
            seat for seat in available if (seat.provider, seat.profile) != key
        ]

    def _ask(self, stage: str, prompt: str, preferred: LiveProvider) -> str:
        seats = self._available(preferred)
        candidates: list[dict[str, object]] | None = None
        if self.rotation is not None and seats:
            seats, candidates = self.rotation.choose(seats, preferred)
        if not seats:
            code = self.exhausted[-1].get("error") if self.exhausted else (
                "rate_limited" if candidates else "provider_unavailable"
            )
            raise ProviderError(str(code))
        if candidates is not None:
            # Why this account: every candidate's standing when the stage began.
            self.selections.append({
                "stage": stage,
                "account": account_key(seats[0].provider, seats[0].profile),
                "candidates": candidates,
            })
        error: ProviderError | None = None
        for seat in seats:
            self.attempted += 1
            try:
                reply = seat.ask(prompt)
            except ProviderError as caught:
                if self.rotation is not None:
                    self.rotation.observe(seat, None, str(caught))
                if not is_transient(str(caught)):
                    raise
                self.exhausted.append({
                    "provider": seat.provider,
                    "profile": seat.profile,
                    "error": str(caught),
                    "stage": stage,
                })
                error = caught
                continue
            if self.rotation is not None:
                self.rotation.observe(seat, reply.usage, None)
                if candidates is not None:
                    self.selections[-1]["account"] = account_key(seat.provider, seat.profile)
            return self._record(stage, seat, reply)
        if error is None:
            raise ProviderError("provider_unavailable")
        raise error

    def _record(self, stage: str, provider: LiveProvider, reply: Reply) -> str:
        self.stages[stage] = {
            "profile": provider.profile,
            "provider": provider.provider,
            "version": provider.version,
            "text": reply.text,
            "usage": reply.usage.as_record(),
        }
        return reply.text

    def plan(self, task: str) -> str:
        self.task = task
        self.plan_text = self._ask("plan", PLAN_PROMPT.format(task=task), self.lead)
        return self.plan_text

    def review(self, plan: str) -> str:
        prompt = REVIEW_PROMPT.format(task=self.task, plan=plan)
        return self._ask("review", prompt, self.reviewer)

    def synthesize(self, review: str) -> str:
        prompt = SYNTHESIS_PROMPT.format(task=self.task, plan=self.plan_text, review=review)
        return self._ask("synthesize", prompt, self.lead)

    def details(self) -> dict[str, object]:
        rotating: dict[str, object] = {}
        if self.rotation is not None:
            rotating = {"rotation": self.rotation.policy.as_record(), "selection": self.selections}
        return {
            **rotating,
            "stages": self.stages,
            "usage": total_usage(self.stages, self.attempted),
            "roster": self.roster(),
            "exhausted": self.exhausted,
            "attempted": self.attempted,
            "task": self.task,
            "workspace": str(self.lead.workspace) if self.lead.workspace is not None else None,
        }


def _saved_stages(stages: object) -> dict[str, dict[str, object]]:
    """Keep finished live stages only when each one has text and usage."""
    if stages is None:
        return {}
    if not isinstance(stages, dict):
        raise ValueError("invalid stage record")
    cleaned: dict[str, dict[str, object]] = {}
    for name in ("plan", "review", "synthesize"):
        if name not in stages:
            continue
        stage = stages[name]
        if not isinstance(stage, dict) or not isinstance(stage.get("text"), str):
            raise ValueError("invalid stage record")
        usage = stage.get("usage")
        if not isinstance(usage, dict) or usage.get("provenance") not in {"measured", "unknown", "partial"}:
            raise ValueError("invalid stage record")
        for key in ("input_tokens", "output_tokens"):
            if key not in usage or not (usage[key] is None or type(usage[key]) is int):
                raise ValueError("invalid stage record")
        if usage["provenance"] == "measured" and (
            usage["input_tokens"] is None or usage["output_tokens"] is None
        ):
            raise ValueError("invalid stage record")
        cleaned[name] = dict(stage)
    return cleaned


def _saved_exhausted(exhausted: object) -> list[dict[str, object]]:
    """Reject an entry that would not match a seat and would let that seat run again."""
    if not isinstance(exhausted, list):
        raise ValueError("invalid exhausted provider list")
    cleaned: list[dict[str, object]] = []
    for item in exhausted:
        if not isinstance(item, dict):
            raise ValueError("invalid exhausted provider list")
        provider = item.get("provider")
        profile = item.get("profile")
        error = item.get("error")
        if (
            provider not in PROVIDERS
            or (profile is not None and not isinstance(profile, str))
            or not isinstance(error, str)
            or not error
        ):
            raise ValueError("invalid exhausted provider list")
        cleaned.append(dict(item))
    return cleaned


def total_usage(stages: dict[str, dict[str, object]], attempted: int | None = None) -> dict[str, object]:
    """Sum measured counts. Mark the total partial when any attempted stage lacks counts."""
    usages: list[dict[str, Any]] = [stage["usage"] for stage in stages.values()]  # type: ignore[misc]
    measured = [usage for usage in usages if usage["provenance"] == "measured"]
    if not measured:
        return Usage().as_record()
    return {
        "provenance": "measured" if len(measured) == max(len(usages), attempted or 0) else "partial",
        "input_tokens": sum(usage["input_tokens"] for usage in measured),
        "output_tokens": sum(usage["output_tokens"] for usage in measured),
    }


def describe(providers: dict[str, LiveProvider]) -> dict[str, object]:
    """Dry-run view of the live roles."""
    return {
        role: {
            "provider": provider.provider,
            "workspace": str(provider.workspace) if provider.workspace else "temporary",
            "profile": provider.profile,
            **{key: value for key, value in readiness(provider.provider).items() if key != "executable"},
        }
        for role, provider in providers.items()
    }


def dumps(value: object) -> str:
    return json.dumps(value, indent=2)
