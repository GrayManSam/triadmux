"""Read an account's real remaining usage from the provider's own CLI.

Firstchair asks the signed-in CLI and never opens a credential file itself.

- Codex CLI: ``codex app-server`` answers the JSON-RPC request
  ``account/rateLimits/read`` with the percent used and the reset time of
  each usage window. Checked against codex-cli 0.171.0 and its generated
  schema (``codex app-server generate-json-schema``). No prompt is sent.
- Claude Code: a print-mode call with ``--output-format stream-json`` emits
  a ``rate_limit_event`` whose ``unifiedWindows`` hold the utilization and
  reset time of the five-hour and seven-day windows. Checked against Claude
  Code 2.1.330. Claude Code has no usage command, so this reader sends one
  short fixed prompt with no tools. That prompt uses a little of the account.
- Grok Build 1.0.77: ``grok usage --json`` refuses to run without Grok's
  interactive background service, so Grok has no reader.

A reader that fails for any reason reports nothing. Callers then fall back
to what Firstchair counted itself.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

from .provider import ProviderError
from .versions import check_version

_READ_LIMIT = 1024 * 1024
READERS = ("codex", "claude")
# Reading these spends a little of the account, so they are refreshed less often.
PROBE_COSTS_USAGE = ("claude",)
_CLAUDE_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 7 * 1440}
_CLAUDE_PROBE = (
    "-p", "Reply with exactly FIRSTCHAIR_USAGE_OK. Do not use any tools.",
    "--tools", "", "--setting-sources", "", "--no-session-persistence",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--output-format", "stream-json", "--verbose",
)


@dataclass(frozen=True)
class Window:
    used_percent: int
    minutes: int | None
    resets_at: int | None


@dataclass(frozen=True)
class Quota:
    """One reading. ``allowed`` is the provider's own verdict when it gives one."""

    windows: tuple[Window, ...]
    allowed: bool | None
    observed_at: float

    def as_record(self) -> dict[str, object]:
        return {
            "windows": [[w.used_percent, w.minutes, w.resets_at] for w in self.windows],
            "allowed": self.allowed,
            "observed_at": self.observed_at,
        }


def _whole(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value < 1 << 53:
        raise ValueError("invalid quota")
    return value


def read_record(saved: object) -> Quota:
    """Rebuild a saved reading. Anything unexpected is refused."""
    if not isinstance(saved, dict) or set(saved) != {"windows", "allowed", "observed_at"}:
        raise ValueError("invalid quota")
    windows, allowed, observed = saved["windows"], saved["allowed"], saved["observed_at"]
    if not isinstance(windows, list) or len(windows) > 8 or not (allowed is None or type(allowed) is bool):
        raise ValueError("invalid quota")
    if type(observed) not in (int, float) or not math.isfinite(observed):
        raise ValueError("invalid quota")
    parsed = []
    for item in windows:
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError("invalid quota")
        parsed.append(Window(
            _whole(item[0], optional=False) or 0, _whole(item[1], optional=True), _whole(item[2], optional=True),
        ))
    return Quota(tuple(parsed), allowed, float(observed))


def parse_codex_rate_limits(result: object, now: float) -> Quota:
    """Read the reply to ``account/rateLimits/read``. The account ID is not kept."""
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        raise ValueError("invalid quota")
    allowed = result.get("ordinaryUsageAllowed")
    if not (allowed is None or type(allowed) is bool):
        raise ValueError("invalid quota")
    windows = []
    for name in ("primary", "secondary"):
        window = result["rateLimits"].get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError("invalid quota")
        windows.append(Window(
            _whole(window.get("usedPercent"), optional=False) or 0,
            _whole(window.get("windowDurationMins"), optional=True),
            _whole(window.get("resetsAt"), optional=True),
        ))
    if not windows and allowed is None:
        raise ValueError("invalid quota")
    return Quota(tuple(windows), allowed, now)


def _rpc(argv: list[str], env: dict[str, str], requests: list[dict[str, object]], answer_id: int, timeout: float) -> object:
    """Send newline-delimited JSON-RPC and return the result for ``answer_id``."""
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, start_new_session=os.name == "posix",
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write("".join(json.dumps(item) + "\n" for item in requests).encode())
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        buffer = b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline and len(buffer) <= _READ_LIMIT:
                if not selector.select(max(0.0, min(0.5, deadline - time.monotonic()))):
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("id") == answer_id:
                        if "result" not in message:
                            raise ValueError("invalid quota")
                        return message["result"]
        raise ValueError("invalid quota")
    finally:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        process.wait()


def read_codex_quota(executable: str, env: dict[str, str], *, timeout: float = 15.0) -> Quota:
    result = _rpc(
        [executable, "app-server"], env,
        [
            {"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "firstchair", "title": "Firstchair", "version": "0"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        ],
        2, timeout,
    )
    return parse_codex_rate_limits(result, time.time())


def parse_claude_stream(output: bytes, now: float) -> Quota:
    """Read the last ``rate_limit_event`` of a stream-json run."""
    info: dict[str, object] | None = None
    refused = False
    for line in output.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("type") == "rate_limit_event" and isinstance(message.get("rate_limit_info"), dict):
            info = message["rate_limit_info"]
        elif message.get("type") == "result" and message.get("api_error_status") == 429:
            refused = True
    if info is None:
        if refused:
            return Quota((), False, now)
        raise ValueError("invalid quota")
    windows = []
    unified = info.get("unifiedWindows")
    if unified is not None and not isinstance(unified, dict):
        raise ValueError("invalid quota")
    for name, window in sorted((unified or {}).items()):
        if not isinstance(window, dict):
            raise ValueError("invalid quota")
        used = window.get("utilization")
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            raise ValueError("invalid quota")
        windows.append(Window(
            min(100, round(used * 100)), _CLAUDE_WINDOW_MINUTES.get(name),
            _whole(window.get("resetsAt"), optional=True),
        ))
    status = info.get("status")
    if not isinstance(status, str):
        raise ValueError("invalid quota")
    if status == "rejected" and not windows:
        # No window detail: keep the reset time the event itself carries.
        windows.append(Window(100, None, _whole(info.get("resetsAt"), optional=True)))
    return Quota(tuple(windows), False if refused or status == "rejected" else True, now)


def read_claude_quota(executable: str, env: dict[str, str], *, timeout: float = 60.0) -> Quota:
    with tempfile.TemporaryDirectory(prefix="firstchair-quota-") as scratch:
        process = subprocess.Popen(
            [executable, *_CLAUDE_PROBE], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=scratch, start_new_session=os.name == "posix",
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        finally:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
    if len(output) > _READ_LIMIT:
        raise ValueError("invalid quota")
    return parse_claude_stream(output, time.time())


def read_quota(provider: str, profile: str | None, *, timeout: float = 15.0) -> Quota | None:
    """The account's real usage, or None when this provider cannot be asked."""
    if provider not in READERS:
        return None
    from .live import find_executable, live_environment, provider_version  # live imports rotation.

    try:
        env = live_environment(provider, profile)
        executable = find_executable(provider, env)
        check_version(provider, provider_version(provider, env, executable))
        if provider == "claude":
            return read_claude_quota(executable, env, timeout=max(timeout, 60.0))
        return read_codex_quota(executable, env, timeout=timeout)
    except (OSError, ValueError, ProviderError, subprocess.SubprocessError):
        return None
