"""Pick which signed-in account runs the next stage.

Where the provider's CLI reports it, the ledger holds each account's real
usage: the percent used and reset time of every usage window (see
``quota.py``). It also holds what Firstchair counted itself, the tokens and
calls it sent in a rolling window, and when a provider last refused an
account. Before each stage the accounts are ranked:

1. An account is not used while it rests after a usage failure, or while
   the provider reports a window full or the account refused.
2. An account under the switch point is ``ready``. One at or past it is
   ``over_budget`` and is used only when no ready account of that provider
   is left. The switch point is a percent of the provider's real window,
   plus the optional token and call budgets the user set.
3. Accounts of the provider that holds the role come before other providers.
4. The account this run already used for that provider stays in place while
   it is ready, so a run does not hop between accounts for no reason.
5. Otherwise the account with the lowest real percent used goes first, then
   the fewest counted tokens, then the fewest calls.

An account of a provider that reports usage but gave no reading goes after
the accounts that have one. For a provider with no reader, counted tokens
and calls, the user's budgets, and refusals decide.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .accounts import private_directory, profile_root
from .fallback import is_transient
from .parallel import run_bounded
from .quota import PROBE_COSTS_USAGE, READERS, Quota, read_record
from .usage import MAX_TOKEN_COUNT, Usage
from .workflow import save

MAX_ACCOUNTS = 100
_MAX_EVENTS = 10_000


class Seat(Protocol):
    provider: str
    profile: str | None


@dataclass(frozen=True)
class Policy:
    """The user's switching rules. A limit of None never triggers a switch."""

    window_seconds: float = 5 * 3600.0
    token_limit: int | None = None
    call_limit: int | None = None
    cooldown_seconds: float = 3600.0
    # Leave an account when any real usage window reaches this percent.
    switch_percent: int = 90
    # Ask the provider again when a reading is older than this many seconds.
    quota_max_age: float = 120.0
    # The same, for a provider whose reading spends a little usage.
    costly_quota_max_age: float = 900.0

    def __post_init__(self) -> None:
        if type(self.switch_percent) is not int or not 1 <= self.switch_percent <= 100:
            raise ValueError("switch_percent must be an integer from 1 through 100")
        for name in ("window_seconds", "cooldown_seconds", "quota_max_age", "costly_quota_max_age"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 31 * 86400:
                raise ValueError(f"{name} must be greater than 0 and at most 31 days")
        for name in ("token_limit", "call_limit"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 < value <= MAX_TOKEN_COUNT):
                raise ValueError(f"{name} must be a positive integer")

    def as_record(self) -> dict[str, object]:
        return asdict(self)


def read_policy(saved: object) -> Policy:
    """Rebuild the policy a run was started with."""
    if not isinstance(saved, dict) or set(saved) != set(Policy.__dataclass_fields__):
        raise ValueError("invalid rotation policy")
    return Policy(**saved)


def account_key(provider: str, profile: str | None) -> str:
    """``codex`` is the default sign-in. ``codex:work`` is a named one."""
    return provider if profile is None else f"{provider}:{profile}"


def ledger_path() -> Path:
    """One ledger for the user, shared by every checkout and session."""
    return profile_root().parent / "account-ledger.json"


@contextmanager
def _locked(path: Path) -> Generator[None]:
    private_directory(path.parent, create=True)
    if os.name != "posix":
        yield
        return
    import fcntl

    handle = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def read_ledger(path: Path) -> dict[str, dict[str, object]]:
    """Return the accounts in the ledger. A ledger that cannot be trusted is refused."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValueError("invalid account ledger") from None
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(accounts, dict):
        raise ValueError("invalid account ledger")
    for entry in accounts.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("events"), list):
            raise ValueError("invalid account ledger")
        for event in entry["events"]:
            if (
                not isinstance(event, list) or len(event) != 2
                or type(event[0]) not in (int, float) or not math.isfinite(event[0])
                or not (event[1] is None or (type(event[1]) is int and 0 <= event[1] <= MAX_TOKEN_COUNT))
            ):
                raise ValueError("invalid account ledger")
        until = entry.get("cooldown_until")
        if until is not None and (type(until) not in (int, float) or not math.isfinite(until)):
            raise ValueError("invalid account ledger")
        if not (entry.get("last_error") is None or isinstance(entry.get("last_error"), str)):
            raise ValueError("invalid account ledger")
        if entry.get("quota") is not None:
            try:
                read_record(entry["quota"])
            except ValueError:
                raise ValueError("invalid account ledger") from None
    return accounts


def record(
    path: Path, key: str, policy: Policy, *, now: float,
    usage: Usage | None = None, error: str | None = None,
) -> None:
    """Add one stage result to the ledger under the ledger lock."""
    with _locked(path):
        accounts = read_ledger(path)
        entry = accounts.setdefault(key, {"events": [], "cooldown_until": None, "last_error": None})
        events = [event for event in entry["events"] if event[0] > now - policy.window_seconds]  # type: ignore[union-attr]
        if error is not None:
            if is_transient(error):
                entry["cooldown_until"] = now + policy.cooldown_seconds
                entry["last_error"] = error
        else:
            tokens = None
            if usage is not None and usage.provenance == "measured":
                tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
            events.append([now, tokens])
            entry["cooldown_until"] = None
            entry["last_error"] = None
        entry["events"] = events[-_MAX_EVENTS:]
        save(path, {"schema_version": 1, "accounts": accounts})


def store_quota(path: Path, readings: dict[str, Quota]) -> None:
    """Save real usage readings under the ledger lock."""
    if not readings:
        return
    with _locked(path):
        accounts = read_ledger(path)
        for key, quota in readings.items():
            entry = accounts.setdefault(key, {"events": [], "cooldown_until": None, "last_error": None})
            entry["quota"] = quota.as_record()
        save(path, {"schema_version": 1, "accounts": accounts})


def clear_cooldowns(path: Path, keys: Sequence[str] | None = None) -> list[str]:
    """Forget cooldowns, for all accounts or the named ones. Usage is kept."""
    cleared = []
    with _locked(path):
        accounts = read_ledger(path)
        for key, entry in accounts.items():
            if (keys is None or key in keys) and entry.get("cooldown_until") is not None:
                entry["cooldown_until"] = None
                entry["last_error"] = None
                cleared.append(key)
        if cleared:
            save(path, {"schema_version": 1, "accounts": accounts})
    return sorted(cleared)


def standing(
    key: str, accounts: dict[str, dict[str, object]], policy: Policy, now: float,
) -> dict[str, object]:
    """What Firstchair knows about one account inside the window."""
    entry = accounts.get(key, {})
    events = [event for event in entry.get("events", []) if event[0] > now - policy.window_seconds]  # type: ignore[union-attr]
    tokens = sum(event[1] or 0 for event in events)
    until = entry.get("cooldown_until")
    cooling = max(0.0, until - now) if isinstance(until, (int, float)) else 0.0
    # Real usage. A window whose reset time has passed counts as empty.
    percent: int | None = None
    resets: float | None = None
    refused = False
    age: float | None = None
    if entry.get("quota") is not None:
        quota = read_record(entry["quota"])
        age = max(0.0, now - quota.observed_at)
        live = [w for w in quota.windows if w.resets_at is None or w.resets_at > now]
        percent = max((w.used_percent for w in live), default=0)
        full = [w.resets_at for w in live if w.used_percent >= 100 and w.resets_at is not None]
        pending = [w.resets_at for w in live if w.resets_at is not None]
        # A refusal lasts until the next window resets. Without a reset time
        # it lasts only while the reading is fresh.
        refused = quota.allowed is False and (bool(pending) or age <= policy.quota_max_age)
        if full or (refused and pending):
            resets = float(min(full or pending))
    if cooling > 0:
        status = "cooling_down"
    elif refused or (percent is not None and percent >= 100):
        status = "exhausted"
    elif (
        (percent is not None and percent >= policy.switch_percent)
        or (policy.token_limit is not None and tokens >= policy.token_limit)
        or (policy.call_limit is not None and len(events) >= policy.call_limit)
    ):
        status = "over_budget"
    else:
        status = "ready"
    return {
        "account": key,
        "status": status,
        "used_percent": percent,
        "usage_source": "provider" if percent is not None else "counted",
        "reading_age_seconds": None if age is None else round(age),
        "resets_in_seconds": None if resets is None else max(0, round(resets - now)),
        "tokens": tokens,
        "calls": len(events),
        "unmeasured_calls": sum(1 for event in events if event[1] is None),
        "cooldown_seconds": round(cooling),
        "last_used": max((event[0] for event in events), default=None),
        "last_error": entry.get("last_error") if cooling > 0 else None,
    }


def rank[S: Seat](
    seats: Sequence[S], preferred_provider: str, sticky: dict[str, str],
    accounts: dict[str, dict[str, object]], policy: Policy, now: float,
) -> tuple[list[S], list[dict[str, object]]]:
    """Order the usable seats and report why. Cooling seats are left out."""
    report = [standing(account_key(seat.provider, seat.profile), accounts, policy, now) for seat in seats]

    def order(index: int) -> tuple[object, ...]:
        seat, state = seats[index], report[index]
        return (
            seat.provider != preferred_provider,
            state["status"] != "ready",
            sticky.get(seat.provider) != state["account"],
            # A provider that reports usage gave nothing for this account. It
            # may not be signed in, so accounts with a reading go first.
            state["used_percent"] is None and seat.provider in READERS,
            state["used_percent"] or 0,
            state["tokens"], state["calls"], state["last_used"] or 0.0, index,
        )

    usable = sorted(
        (index for index, state in enumerate(report)
         if state["status"] not in ("cooling_down", "exhausted")), key=order,
    )
    for place, index in enumerate(usable, start=1):
        report[index]["rank"] = place
    return [seats[index] for index in usable], report


class Rotation:
    """The ledger-backed chooser one run uses for every stage.

    ``reader`` asks a provider for an account's real usage and returns None
    when it cannot. Without a reader only counted usage is used.
    """

    def __init__(
        self, path: Path, policy: Policy, clock: Callable[[], float] = time.time,
        reader: Callable[[Seat], Quota | None] | None = None,
    ) -> None:
        self.path, self.policy, self.clock, self.reader = path, policy, clock, reader
        self.sticky: dict[str, str] = {}
        # Accounts whose provider gave no reading. They are not asked again in this run.
        self.unreadable: set[str] = set()

    def refresh(self, seats: Sequence[Seat], *, force: bool = False) -> None:
        """Read real usage for accounts whose saved reading is missing or old."""
        if self.reader is None:
            return
        accounts, now = read_ledger(self.path), self.clock()
        stale: dict[str, Seat] = {}
        for seat in seats:
            key = account_key(seat.provider, seat.profile)
            if key in self.unreadable and not force:
                continue
            limit = (
                self.policy.costly_quota_max_age if seat.provider in PROBE_COSTS_USAGE
                else self.policy.quota_max_age
            )
            saved = accounts.get(key, {}).get("quota")
            if force or saved is None or now - read_record(saved).observed_at > limit:
                stale[key] = seat
        reader = self.reader
        keys = list(stale)
        readings: dict[str, Quota] = {}
        for start in range(0, len(keys), 4):
            wave = keys[start:start + 4]
            found = run_bounded([lambda key=key: reader(stale[key]) for key in wave], limit=len(wave))
            readings.update({key: quota for key, quota in zip(wave, found) if quota is not None})
            self.unreadable.update(key for key, quota in zip(wave, found) if quota is None)
        store_quota(self.path, readings)

    def choose[S: Seat](self, seats: Sequence[S], preferred: Seat) -> tuple[list[S], list[dict[str, object]]]:
        # Only the role's own provider is asked. Other providers are a fallback.
        self.refresh([seat for seat in seats if seat.provider == preferred.provider])
        return rank(seats, preferred.provider, self.sticky, read_ledger(self.path), self.policy, self.clock())

    def observe(self, seat: Seat, usage: Usage | None, error: str | None) -> None:
        key = account_key(seat.provider, seat.profile)
        record(self.path, key, self.policy, now=self.clock(), usage=usage, error=error)
        if error is None:
            self.sticky[seat.provider] = key
