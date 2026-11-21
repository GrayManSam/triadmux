"""Command-line entry point."""

import argparse
import json
import sys
from time import monotonic
from functools import partial
from pathlib import Path
from typing import cast

from . import __version__, accounts
from .config import read_synthetic_config
from .conductor import configure_parser as configure_conduct_parser
from .conductor import execute as execute_conduct
from .delegation import configure_parser as configure_delegate_parser
from .delegation import execute as execute_delegation
from .inventory import provider_inventory, routing_explanation
from .live import PROVIDERS as LIVE_PROVIDERS
from .live import LiveLimits, LiveProvider, LiveSession, describe, readiness
from .parallel import run_bounded
from .provider import ExecutionLimits, ProviderError, SyntheticProvider
from .quota import read_quota
from .rotation import MAX_ACCOUNTS, Policy, Rotation, ledger_path, read_policy
from .sessions import SESSION_ENV, require_owner, resolve_session
from .state_listing import list_runs
from .workflow import (
    continue_workflow,
    demo_plan,
    demo_review,
    demo_synthesize,
    recover_workflow,
    record_path,
    run_workflow,
)

STAGES = ("plan", "review", "synthesize")
STATE_DIR = Path(".firstchair")
LEGACY_STATE_DIR = Path(".triadmux")
LEGACY_NOTICE = (
    "Firstchair replaces triadmux. "
    "The triadmux command will be removed in the next minor release."
)


def default_state_dir() -> Path:
    """Read the former TriadMux state directory until the new one exists."""
    if not STATE_DIR.exists() and LEGACY_STATE_DIR.is_dir():
        print(f"Reading former state directory {LEGACY_STATE_DIR}/", file=sys.stderr)
        return LEGACY_STATE_DIR
    return STATE_DIR


def legacy_main(argv: list[str] | None = None) -> int:
    print(LEGACY_NOTICE, file=sys.stderr)
    return main(argv, prog="triadmux")


def _read_seat_quota(seat: LiveProvider) -> object:
    return read_quota(seat.provider, seat.profile)


def _fallback_seat(text: str) -> tuple[str, str | None]:
    """Read --fallback PROVIDER or PROVIDER:PROFILE."""
    name, separator, profile = text.partition(":")
    if name not in LIVE_PROVIDERS or (separator and not profile):
        raise argparse.ArgumentTypeError(
            f"expected one of {', '.join(LIVE_PROVIDERS)}, optionally followed by :PROFILE"
        )
    return name, profile or None


def main(argv: list[str] | None = None, prog: str = "firstchair") -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--session",
        help=f"This lead session's ID (default: ${SESSION_ENV}; run makes one when unset)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Discover providers and check their versions")
    doctor.add_argument("--offline", action="store_true", help="Only search PATH; do not run --version")
    run = commands.add_parser("run", help="Run a plan, review and synthesis workflow")
    modes = run.add_mutually_exclusive_group(required=True)
    modes.add_argument("--demo", action="store_true")
    modes.add_argument(
        "--provider", choices=("synthetic", *LIVE_PROVIDERS),
        help="Lead provider. Live providers run read-only with your existing sign-in",
    )
    run.add_argument("--reviewer", choices=LIVE_PROVIDERS, help="Live reviewer provider (default: the lead)")
    run.add_argument(
        "--fallback", action="append", type=_fallback_seat, default=None, metavar="PROVIDER[:PROFILE]",
        help="Extra provider, or a second named account, used when an earlier one is out of usage",
    )
    run.add_argument(
        "--rotate-accounts", action="store_true",
        help="Use every named sign-in of the chosen providers and pick the best one for each stage",
    )
    run.add_argument(
        "--account-switch-percent", type=int,
        help="Move off an account when its real usage reaches this percent (default 90)",
    )
    run.add_argument("--account-token-limit", type=int, help="Move off an account after this many tokens in the window")
    run.add_argument("--account-call-limit", type=int, help="Move off an account after this many stages in the window")
    run.add_argument("--account-window", type=float, help="Seconds the limits look back (default 18000)")
    run.add_argument("--account-cooldown", type=float, help="Seconds to rest an account that ran out of usage (default 3600)")
    run.add_argument("--workspace", type=Path, help="Directory live providers may read (default: an empty temp dir)")
    run.add_argument("--profile", help="Named account profile for the lead (see firstchair auth)")
    run.add_argument("--reviewer-profile", help="Named account profile for the reviewer")
    run.add_argument("--timeout", type=float, default=None, help="Per-stage timeout in seconds")
    run.add_argument("--max-output-bytes", type=int, default=None, help="Provider output limit")
    run.add_argument("--max-input-bytes", type=int, help="Maximum bytes in each provider request")
    run.add_argument("--total-timeout", type=float, help="Time budget across all stages")
    run.add_argument("--config", type=Path, help="TOML file with synthetic execution limits")
    run.add_argument("--dry-run", action="store_true", help="Show the execution plan without running it")
    run.add_argument("--format", choices=("json", "text"), default="json", help="Output format")
    run.add_argument("task", help="Task text, or - to read it from stdin")
    status = commands.add_parser("status", help="Read a run record")
    status.add_argument("run_id")
    commands.add_parser("runs", help="List saved run summaries")
    recover = commands.add_parser("recover", help="Mark an abandoned run interrupted")
    recover.add_argument("run_id")
    resume = commands.add_parser("resume", help="Continue a stopped run from the last finished stage")
    resume.add_argument("run_id")
    resume.add_argument("--provider", choices=LIVE_PROVIDERS, help="Provider to add when the saved ones are exhausted")
    resume.add_argument("--reviewer", choices=LIVE_PROVIDERS)
    resume.add_argument(
        "--fallback", action="append", type=_fallback_seat, default=None, metavar="PROVIDER[:PROFILE]",
    )
    resume.add_argument(
        "--retry-exhausted", action="store_true",
        help="Try the providers this run retired again, for example after a limit reset",
    )
    resume.add_argument("--workspace", type=Path)
    resume.add_argument("--profile")
    resume.add_argument("--reviewer-profile")
    resume.add_argument("--timeout", type=float, default=None)
    resume.add_argument("--max-output-bytes", type=int, default=None)
    resume.add_argument("--max-input-bytes", type=int)
    resume.add_argument("--total-timeout", type=float)
    resume.add_argument("--format", choices=("json", "text"), default="json")
    delegate = commands.add_parser("delegate", help="Dispatch workers in isolated Git worktrees")
    configure_delegate_parser(delegate)
    conduct = commands.add_parser("conduct", help="Run a score of workers from the primary seat")
    configure_conduct_parser(conduct)
    auth = commands.add_parser("auth", help="Manage named provider logins")
    accounts.configure_parser(auth)
    args = parser.parse_args(argv)
    if args.command == "auth":
        return accounts.execute(args)
    if args.command == "delegate":
        return execute_delegation(args)
    if args.command == "conduct":
        return execute_conduct(args)
    if args.state_dir is None:
        args.state_dir = default_state_dir()

    try:
        if args.command == "doctor":
            providers = provider_inventory()
            if not args.offline:
                def probe(name: str, current: str | bool | None) -> dict[str, str | bool | None]:
                    report = readiness(name)
                    contract = report["contract"]
                    supported = contract in ("verified", "compatible")
                    return {
                        "version": cast(str | None, report["version"]),
                        "contract": cast(str | None, contract),
                        "readiness": "ready" if supported else current,
                        "enabled": supported,
                    }

                probed = run_bounded(
                    [partial(probe, name, providers[name]["readiness"]) for name in LIVE_PROVIDERS],
                    limit=3,
                )
                for name, fields in zip(LIVE_PROVIDERS, probed, strict=True):
                    providers[name].update(fields)
            result = {"providers": providers}
        elif args.command == "run":
            if args.task == "-":
                args.task = sys.stdin.read()
            if not args.task.strip():
                raise ValueError("task must contain text")
            live = args.provider in LIVE_PROVIDERS
            tuning = (
                args.account_token_limit, args.account_call_limit, args.account_window, args.account_cooldown,
                args.account_switch_percent,
            )
            live_only = (
                args.reviewer, args.workspace, args.profile, args.reviewer_profile, args.fallback,
                args.rotate_accounts or None, *tuning,
            )
            if not live and any(value is not None for value in live_only):
                raise ValueError(
                    "--reviewer, --workspace, --profile, --fallback and account rotation "
                    "require a live --provider"
                )
            if not args.rotate_accounts and any(value is not None for value in tuning):
                raise ValueError("the --account-* limits require --rotate-accounts")
            overrides = {
                "timeout": args.timeout,
                "max_output_bytes": args.max_output_bytes,
                "max_input_bytes": args.max_input_bytes,
                "total_timeout": args.total_timeout,
            }
            overrides = {key: value for key, value in overrides.items() if value is not None}
            details = None
            if live:
                if args.config is not None:
                    raise ValueError("--config applies to --provider synthetic")
                defaults = LiveLimits()
                limits = LiveLimits(
                    timeout=overrides.get("timeout", defaults.timeout),
                    output_bytes=overrides.get("max_output_bytes", defaults.output_bytes),
                    input_bytes=overrides.get("max_input_bytes", defaults.input_bytes),
                )
                total = overrides.get("total_timeout")
                if total is not None and not 0 < total <= 4 * 3600:
                    raise ValueError("total timeout must be greater than 0 and at most 14400 seconds")
                deadline = None if total is None else monotonic() + total
                lead = LiveProvider(args.provider, limits, args.workspace, args.profile, deadline)
                reviewer_name = args.reviewer or args.provider
                if reviewer_name == args.provider and args.reviewer_profile in (None, args.profile):
                    reviewer = lead
                else:
                    reviewer = LiveProvider(
                        reviewer_name, limits, args.workspace,
                        args.reviewer_profile or (args.profile if reviewer_name == args.provider else None),
                        deadline,
                    )
                extras = tuple(
                    LiveProvider(name, limits, args.workspace, profile, deadline)
                    for name, profile in (args.fallback or [])
                )
                session = LiveSession(lead, reviewer, extras)
                if args.rotate_accounts:
                    policy = Policy(**{
                        key: value for key, value in {
                            "token_limit": args.account_token_limit,
                            "call_limit": args.account_call_limit,
                            "window_seconds": args.account_window,
                            "cooldown_seconds": args.account_cooldown,
                            "switch_percent": args.account_switch_percent,
                        }.items() if value is not None
                    })
                    # Every named sign-in of a provider this run already names joins the pool.
                    named = {seat.provider for seat in session.seats()}
                    have = {(seat.provider, seat.profile) for seat in session.seats()}
                    pool = tuple(
                        LiveProvider(item["provider"], limits, args.workspace, item["profile"], deadline)
                        for item in accounts.list_profiles()
                        if item["provider"] in named and (item["provider"], item["profile"]) not in have
                    )
                    session = LiveSession(
                        lead, reviewer, extras + pool,
                        rotation=Rotation(ledger_path(), policy, reader=_read_seat_quota),
                    )
                    if len(session.seats()) > MAX_ACCOUNTS:
                        raise ValueError(f"at most {MAX_ACCOUNTS} accounts can share a run")
                elif len(session.seats()) > 3:
                    raise ValueError("at most 3 providers can share a run; add --rotate-accounts for more")
                stages = (session.plan, session.review, session.synthesize)
                details = session.details
            elif args.provider:
                settings = read_synthetic_config(args.config) if args.config is not None else {}
                settings.update(overrides)
                defaults = ExecutionLimits()
                limits = ExecutionLimits(
                    timeout=settings.get("timeout", defaults.timeout),
                    output_bytes=settings.get("max_output_bytes", defaults.output_bytes),
                    input_bytes=settings.get("max_input_bytes", defaults.input_bytes),
                )
                provider = SyntheticProvider(limits, total_timeout=settings.get("total_timeout"))
                stages = tuple(partial(provider.run, stage) for stage in STAGES)
            else:
                if overrides or args.config is not None:
                    raise ValueError("execution configuration requires --provider")
                stages = (demo_plan, demo_review, demo_synthesize)
            if args.dry_run and live:
                result = {
                    "status": "planned",
                    "provider": args.provider,
                    "selection": "explicit",
                    "roles": describe({"lead": lead, "reviewer": reviewer}),
                    "stages": {"plan": "lead", "review": "reviewer", "synthesize": "lead"},
                    "failover": [seat.provider for seat in session.seats()],
                    **({
                        "rotation": session.rotation.policy.as_record(),
                        "accounts": session.rotation.choose(session.seats(), lead)[1],
                    } if session.rotation is not None else {}),
                    "limits": {
                        "stage_timeout": limits.timeout,
                        "stage_output_bytes": limits.output_bytes,
                        "stage_input_bytes": limits.input_bytes,
                        "total_timeout": total,
                    },
                    "permissions": "read-only",
                }
            elif args.dry_run:
                result = {
                    "status": "planned",
                    "provider": args.provider or "demo",
                    "selection": "explicit",
                    "routing": routing_explanation(args.provider or "demo"),
                    "stages": STAGES,
                }
                if args.provider:
                    result["limits"] = {
                        "stage_timeout": limits.timeout,
                        "stage_output_bytes": limits.output_bytes,
                        "stage_input_bytes": limits.input_bytes,
                        "total_timeout": provider.total_timeout,
                    }
                    # Provider stages use a controller temp dir, not the repo cwd.
                    result["workspace"] = "temporary"
            else:
                if live:
                    # Reject an unsupported CLI before a run record is created.
                    session.task = args.task
                    for provider in session.seats():
                        try:
                            provider.prepare()
                        except ProviderError as error:
                            raise ValueError(
                                f"{provider.provider} is not ready: {error}. Run firstchair doctor"
                            ) from None
                result = run_workflow(
                    args.task, args.state_dir, *stages, details=details,
                    provider=args.provider or "demo",
                    prelude=session.details() if live else None,
                    session=resolve_session(args.session, create=True),
                )
        elif args.command == "resume":
            result = _resume(args)
        elif args.command == "recover":
            result = recover_workflow(
                args.state_dir, args.run_id, session=resolve_session(args.session),
            )
        elif args.command == "runs":
            result = {"runs": list_runs(args.state_dir)}
        else:
            path = record_path(args.state_dir, args.run_id)
            result = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("invalid run record")
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1

    if args.command in {"run", "resume"} and args.format == "text" and "run_id" in result:
        _print_text(result)
    else:
        print(json.dumps(result, indent=2))
    if args.command == "recover":
        return 0
    return int(result.get("status") in {"failed", "interrupted"})


def _print_text(record: dict[str, object]) -> None:
    """Human-readable run summary. The JSON record stays on disk."""
    stages = record.get("stages")
    if isinstance(stages, dict):
        for name in ("plan", "review"):
            stage = stages.get(name)
            if isinstance(stage, dict):
                print(f"## {name.title()} ({stage.get('provider')})\n\n{stage.get('text', '').strip()}\n")
    if record.get("status") == "completed":
        print(f"## Result\n\n{str(record.get('result', '')).strip()}\n")
    else:
        print(f"## {record.get('status')}: {record.get('error', 'unknown')}\n")
    exhausted = record.get("exhausted")
    if isinstance(exhausted, list) and exhausted:
        skipped = ", ".join(
            f"{item['provider']} ({item['error']})"
            for item in exhausted
            if (
                isinstance(item, dict)
                and isinstance(item.get("provider"), str)
                and isinstance(item.get("error"), str)
            )
        )
        if skipped:
            print(f"Skipped: {skipped}\n")
    usage = record.get("usage")
    if isinstance(usage, dict) and usage.get("provenance") != "unknown":
        print(f"Tokens ({usage['provenance']}): {usage['input_tokens']} in, {usage['output_tokens']} out")
    print(f"Run {record['run_id']} · firstchair status {record['run_id']}", file=sys.stderr)


def _resume(args: argparse.Namespace) -> dict[str, object]:
    path = record_path(args.state_dir, args.run_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("invalid run record")
    owner = resolve_session(args.session)
    # Checked again under the run lock. This check stops before any provider probe.
    require_owner(record.get("session"), owner, f"run {args.run_id}")
    provider_name = record.get("provider")
    live_request = any(
        value is not None
        for value in (
            args.provider, args.reviewer, args.workspace, args.profile,
            args.reviewer_profile, args.fallback,
        )
    )
    if provider_name == "demo":
        if live_request or args.retry_exhausted:
            raise ValueError("a demo run cannot add or retry a live provider")
        return continue_workflow(
            args.state_dir, args.run_id, demo_plan, demo_review, demo_synthesize, session=owner,
        )
    if provider_name == "synthetic":
        if live_request or args.retry_exhausted:
            raise ValueError("a synthetic run cannot add or retry a live provider")
        overrides = {
            key: value for key, value in {
                "timeout": args.timeout,
                "max_output_bytes": args.max_output_bytes,
                "max_input_bytes": args.max_input_bytes,
                "total_timeout": args.total_timeout,
            }.items() if value is not None
        }
        defaults = ExecutionLimits()
        limits = ExecutionLimits(
            timeout=overrides.get("timeout", defaults.timeout),
            output_bytes=overrides.get("max_output_bytes", defaults.output_bytes),
            input_bytes=overrides.get("max_input_bytes", defaults.input_bytes),
        )
        synthetic = SyntheticProvider(limits, total_timeout=overrides.get("total_timeout"))
        stages = tuple(partial(synthetic.run, stage) for stage in STAGES)
        return continue_workflow(args.state_dir, args.run_id, *stages, session=owner)
    if provider_name not in LIVE_PROVIDERS:
        raise ValueError("run has no saved provider")
    session = _resume_session(args, record)
    spent = session.spent()
    for seat in session.seats():
        if (seat.provider, seat.profile) in spent:
            continue
        try:
            seat.prepare()
        except ProviderError as error:
            raise ValueError(
                f"{seat.provider} is not ready: {error}. Run firstchair doctor"
            ) from None
    return continue_workflow(
        args.state_dir, args.run_id,
        session.plan, session.review, session.synthesize, details=session.details,
        session=owner,
    )


def _resume_session(args: argparse.Namespace, record: dict[str, object]) -> LiveSession:
    overrides = {
        key: value for key, value in {
            "timeout": args.timeout,
            "max_output_bytes": args.max_output_bytes,
            "max_input_bytes": args.max_input_bytes,
            "total_timeout": args.total_timeout,
        }.items() if value is not None
    }
    defaults = LiveLimits()
    limits = LiveLimits(
        timeout=overrides.get("timeout", defaults.timeout),
        output_bytes=overrides.get("max_output_bytes", defaults.output_bytes),
        input_bytes=overrides.get("max_input_bytes", defaults.input_bytes),
    )
    total = overrides.get("total_timeout")
    if total is not None and not 0 < total <= 4 * 3600:
        raise ValueError("total timeout must be greater than 0 and at most 14400 seconds")
    deadline = None if total is None else monotonic() + total
    workspace = args.workspace
    if workspace is None:
        saved = record.get("workspace")
        if isinstance(saved, str) and Path(saved).is_dir():
            workspace = Path(saved)
    specs: list[tuple[str, str | None]] = []
    saved_roles: dict[str, tuple[str, str | None]] = {}
    roster = record.get("roster")
    if isinstance(roster, list):
        for item in roster:
            if not isinstance(item, dict):
                raise ValueError("invalid provider roster")
            name = item.get("provider")
            profile = item.get("profile")
            if not isinstance(name, str) or name not in LIVE_PROVIDERS:
                raise ValueError("invalid provider roster")
            if profile is not None and not isinstance(profile, str):
                raise ValueError("invalid provider roster")
            roles = item.get("roles", [])
            if not isinstance(roles, list) or any(role not in ("lead", "reviewer") for role in roles):
                raise ValueError("invalid provider roster")
            for role in roles:
                if role in saved_roles:
                    raise ValueError("invalid provider roster")
                saved_roles[role] = (name, profile)
            specs.append((name, profile))
    if saved_roles and set(saved_roles) != {"lead", "reviewer"}:
        raise ValueError("invalid provider roster")
    if args.provider is not None:
        specs.append((args.provider, args.profile))
    if args.reviewer is not None:
        specs.append((args.reviewer, args.reviewer_profile))
    specs.extend(args.fallback or [])
    unique: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for spec in specs:
        if spec not in seen:
            seen.add(spec)
            unique.append(spec)
    if not unique:
        raise ValueError("resume needs --provider when the run has no roster")
    rotation = None
    if "rotation" in record:
        # The run was started with account rotation. Keep its rules.
        rotation = Rotation(ledger_path(), read_policy(record["rotation"]), reader=_read_seat_quota)
    if len(unique) > (MAX_ACCOUNTS if rotation is not None else 3):
        raise ValueError("too many providers or accounts share this run")
    seats = {
        spec: LiveProvider(spec[0], limits, workspace, spec[1], deadline) for spec in unique
    }
    if saved_roles:
        # Providers named on resume are extra seats. The saved roles stay put.
        lead, reviewer = seats[saved_roles["lead"]], seats[saved_roles["reviewer"]]
    else:
        # A roster saved without roles: first seat leads, second reviews.
        lead = seats[unique[0]]
        reviewer = seats[unique[1]] if len(unique) > 1 else lead
    extras = tuple(seat for seat in seats.values() if seat is not lead and seat is not reviewer)
    session = LiveSession(lead, reviewer, extras, rotation=rotation)
    session.restore(record)
    if args.retry_exhausted:
        session.exhausted = []
    return session
