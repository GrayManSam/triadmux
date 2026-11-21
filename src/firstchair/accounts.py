"""Named provider homes and native sign-in, with no Firstchair credential store."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HOME_VARIABLE = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "grok": "GROK_HOME"}
LOGIN = {
    "codex": ("codex", "login"),
    "claude": ("claude", "auth", "login", "--claudeai"),
    "grok": ("grok", "login"),
}
CLAUDE_PROBE = (
    "claude", "-p", "Reply with exactly FIRSTCHAIR_SMOKE_OK. Do not use any tools.",
    "--tools", "", "--setting-sources", "", "--no-session-persistence",
    "--output-format", "json",
)
CHECK = {
    "codex": ("codex", "login", "status"),
    "claude": ("claude", "auth", "status", "--json"),
    "grok": ("grok", "--no-auto-update", "-p", "Reply exactly FIRSTCHAIR_AUTH_OK. Use no tools.",
             "--output-format", "plain", "--tools", "", "--no-subagents"),
}
CREDENTIAL_OVERRIDES = {
    "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN", "XAI_API_KEY", "GROK_API_KEY",
}
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def profile_root() -> Path:
    if not hasattr(os, "getuid"):
        raise ValueError("named accounts require POSIX directory permissions")
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not state.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    state = state.resolve()
    if state.exists():
        info = state.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("XDG state directory must be owned by you and protected from other writers")
    root = state / "firstchair" / "accounts"
    markers = (parent / ".git" for parent in (root, *root.parents))
    if any(marker.is_file() or (marker / "HEAD").is_file() for marker in markers):
        raise ValueError("account storage must be outside Git checkouts")
    return root


def private_directory(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("account directories must be owned by you, private, and free of symlinks")


def profile_home(provider: str, name: str, *, create: bool = False) -> Path:
    if not PROFILE_NAME.fullmatch(name):
        raise ValueError("profile names use 1–32 lowercase letters, digits, hyphens, or underscores")
    root = profile_root()
    home = root / provider / name
    for directory in (root.parent, root, home.parent, home):
        try:
            private_directory(directory, create=create)
        except FileNotFoundError:
            raise ValueError("unknown account profile; sign in with firstchair auth login") from None
    return home


def account_environment(provider: str, name: str, *, create: bool = False) -> dict[str, str]:
    home = profile_home(provider, name, create=create)
    env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_OVERRIDES}
    env.update({HOME_VARIABLE[provider]: str(home)})
    return env


def claude_failure(stdout: str) -> str:
    """Report a recognized HTTP status without exposing the provider payload."""
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return "provider_exit"
    if not isinstance(data, dict) or data.get("type") != "result" or data.get("is_error") is not True:
        return "provider_exit"
    status = data.get("api_error_status")
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "authorization_failed"
    return "provider_exit"


def list_profiles() -> list[dict[str, str]]:
    """Every named sign-in on this machine, by provider then name."""
    root = profile_root()
    profiles: list[dict[str, str]] = []
    if root.exists() or root.is_symlink():
        private_directory(root.parent, create=False)
        private_directory(root, create=False)
        for provider in LOGIN:
            directory = root / provider
            if directory.exists() or directory.is_symlink():
                private_directory(directory, create=False)
                for candidate in sorted(directory.iterdir()):
                    profile_home(provider, candidate.name)
                    profiles.append({"provider": provider, "profile": candidate.name})
    return profiles


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="auth_command", required=True)
    login = commands.add_parser("login", help="Sign in through the provider CLI")
    login.add_argument("provider", choices=LOGIN)
    login.add_argument("profile")
    login.add_argument("--device-auth", action="store_true", help="Use device-code sign-in over SSH (Codex and Grok)")
    check = commands.add_parser("check", help="Check a login without printing account information")
    check.add_argument("provider", choices=CHECK)
    check.add_argument("profile", nargs="?")
    check.add_argument("--probe", action="store_true", help="Also test Claude inference with a fixed prompt")
    commands.add_parser("list", help="List local profile aliases")
    usage = commands.add_parser("usage", help="Show what Firstchair sent through each account, and cooldowns")
    usage.add_argument("--window", type=float, default=5 * 3600.0, help="Seconds to look back (default 18000)")
    usage.add_argument(
        "--refresh", action="store_true",
        help="Ask each provider for real usage now. Claude Code is asked with one short prompt per account",
    )
    clear = commands.add_parser("clear-cooldown", help="Let accounts in a cooldown be used again")
    clear.add_argument("account", nargs="*", help="PROVIDER or PROVIDER:PROFILE (default: all)")


def execute(args: argparse.Namespace) -> int:
    try:
        if args.auth_command == "list":
            print(json.dumps({"profiles": list_profiles()}, indent=2))
            return 0
        if args.auth_command in {"usage", "clear-cooldown"}:
            from . import rotation  # Imported here: rotation imports this module.

            path = rotation.ledger_path()
            if args.auth_command == "clear-cooldown":
                cleared = rotation.clear_cooldowns(path, args.account or None)
                print(json.dumps({"cleared": cleared}, indent=2))
                return 0
            policy = rotation.Policy(window_seconds=args.window)
            if args.refresh:
                from .quota import READERS, read_quota

                # Named sign-ins, plus the default sign-in of each provider that can be asked.
                targets = [(item["provider"], item["profile"]) for item in list_profiles()]
                targets += [(provider, None) for provider in READERS if shutil.which(provider)]
                readings = {
                    rotation.account_key(provider, profile): quota
                    for provider, profile in targets
                    if (quota := read_quota(provider, profile)) is not None
                }
                rotation.store_quota(path, readings)
            ledger = rotation.read_ledger(path)
            keys = sorted({
                *ledger,
                *(rotation.account_key(item["provider"], item["profile"]) for item in list_profiles()),
            })
            now = rotation.time.time()
            print(json.dumps({
                "window_seconds": policy.window_seconds,
                "accounts": [rotation.standing(key, ledger, policy, now) for key in keys],
            }, indent=2))
            return 0
        provider = args.provider
        if shutil.which(provider) is None:
            raise ValueError(f"{provider} is not installed")
        login = args.auth_command == "login"
        if login and args.device_auth and provider == "claude":
            raise ValueError("Claude Code uses its native sign-in URL and browser code over SSH")
        if login and not sys.stdin.isatty():
            raise ValueError("open a terminal to complete the provider's interactive sign-in")
        probe = not login and args.probe
        if probe and provider != "claude":
            raise ValueError("--probe is available for Claude Code")
        env = account_environment(provider, args.profile, create=login) if args.profile is not None else None
        with tempfile.TemporaryDirectory(prefix="firstchair-auth-") as cwd:
            if login:
                command = (*LOGIN[provider], "--device-auth") if args.device_auth else LOGIN[provider]
                return int(subprocess.run(command, cwd=cwd, env=env, umask=0o077).returncode != 0)
            result = subprocess.run(
                CLAUDE_PROBE if probe else CHECK[provider], cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, capture_output=True,
                text=True, errors="replace", timeout=30, umask=0o077 if env is not None else -1,
            )
        if probe:
            reason = claude_failure(result.stdout)
            try:
                data = json.loads(result.stdout)
            except (ValueError, RecursionError):
                data = None
            ready = (result.returncode == 0 and isinstance(data, dict)
                     and data.get("type") == "result" and data.get("is_error") is False
                     and data.get("result") == "FIRSTCHAIR_SMOKE_OK")
            status = "ready" if ready else reason
            print(json.dumps({"provider": provider, "status": status}))
            return int(not ready)
        if provider == "claude":
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or type(data.get("loggedIn")) is not bool:
                raise ValueError("Claude login status returned an unexpected response")
            ready = result.returncode == 0 and data["loggedIn"]
        elif provider == "grok":
            ready = result.returncode == 0 and result.stdout.strip() == "FIRSTCHAIR_AUTH_OK"
        else:
            ready = result.returncode == 0
        print(json.dumps({"provider": provider, "status": "authenticated" if ready else "unverified"}))
        return int(not ready)
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "provider authentication check timed out"}), file=sys.stderr)
    except (OSError, ValueError) as error:
        message = str(error) if type(error) is ValueError else "could not read account state or start provider"
        print(json.dumps({"error": message}), file=sys.stderr)
    return 2
