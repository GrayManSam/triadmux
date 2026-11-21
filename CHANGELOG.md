# Changelog

## Unreleased

- The primary seat can run a score with `firstchair conduct`. Seat `self`
  starts that seat's own provider. Seats `codex`, `claude`, and `grok` start
  those CLIs. Parts run one at a time in isolated worktrees, and a failed
  part leaves the rest unstarted. The saved board does not store task text
  or replies.
- `firstchair conduct run --limit N` runs up to N parts at once, from 1
  through 4. A score can set a top-level `"limit"`, and the flag overrides
  it. The default is still 1. Parts run in waves of N. After a wave with a
  failed part, no later wave starts, and parts already running finish. The
  summary and the board stay in input order.
- A part can set `"after"` to the name of an earlier part. Its prompt then
  includes that part's reply, marked as untrusted output and cut at 32,000
  characters. The reply stays in memory. The board stores only the name.
- `firstchair run` records its lead session. `resume` and `recover` are
  refused from a different session. `runs` shows each owner.
- A second account of the same provider is its own seat and takes over
  when the first is out of usage. `--fallback PROVIDER:PROFILE` names one
  directly. `firstchair resume --retry-exhausted` tries the skipped seats
  again, for example after a limit reset.
- `firstchair run --rotate-accounts` pools every named sign-in of the
  providers in the run, up to 100, and picks an account before each stage
  from real usage. Codex usage is read with `codex app-server` request
  `account/rateLimits/read`. Claude Code usage is read from the
  `rate_limit_event` of a `stream-json` call, which sends one short fixed
  prompt per reading. Grok Build has no reader and falls back to counted
  calls and refusals. `--account-switch-percent` (default 90) sets when to
  move off an account. A full window or a refusal rests the account until
  its reset time. `--account-token-limit` and `--account-call-limit` add
  budgets counted by Firstchair. The run record lists the accounts
  considered for each stage. `firstchair auth usage [--refresh]` shows the
  per-user ledger and `auth clear-cooldown` ends a rest.
- `firstchair delegate run --rotate-accounts` and `firstchair conduct run
  --rotate-accounts` pick each worker's account from the same real usage
  readings. The summary and the board record the chosen account.
- Each saved stage records the profile that ran it.
- The README is reorganized around what Firstchair is for, and the package
  metadata gains keywords, classifiers, and project links.
- The saved roster records which seat leads and which reviews, so a resumed
  run keeps the same roles. Providers named on resume are extra seats.
- Removed the unused `fallback.run_stage` helper.
- Every score and delegate job records the lead session that started it.
  Pass `--session ID` or set `FIRSTCHAIR_SESSION`. `delegate run` and
  `conduct run` make an ID when none is given and print it. `conduct
  cancel`, `conduct resume`, `delegate collect`, and `delegate finish` are
  refused from a different session or from none. Work recorded before this
  change has no owner.
- The board records when each part started and ended. While a score is
  running, `conduct status` also reports whether a conductor still holds
  the score lock and how long each running part has run.
- `firstchair conduct cancel NAME` stops the running parts of a live score.
  Those parts are recorded as `cancelled`, later parts as `not_started`, and
  the conductor exits 2.
- `firstchair conduct resume --name NAME --score FILE` continues a score
  that has no live conductor. Completed parts are not run again. Read parts
  that did not complete run again. A write part that left a worktree or
  branch is refused until the lead runs `delegate finish`. A completed
  read part runs again only when an unfinished follow-up needs its reply,
  which is never saved. A follow-up of a completed write part is refused.
  A score lock held by the conductor process shows whether it is still
  alive.
- A Grok write worker is refused unless the run passes `--allow-grok-write`.
  That worker bypasses permission prompts and can run terminal commands with
  network access. This applies to `delegate run` and to `conduct plan`,
  `run`, and `resume`, for seat `grok` and for seat `self` under a Grok
  lead. `auto` no longer selects Grok for a write task, so a Codex lead's
  automatic write worker is now Claude Code only.
- Claude Code can run as a delegated worker under `--restricted`. Read parts
  use Read, Grep, and Glob. Write parts also use Edit and Write. Command
  tools are not enabled. A reply is accepted only by the versioned parser.
- `firstchair delegate run auto` can select Claude Code when the preferred
  worker is not on PATH. `auto` still does not select the lead. Grok workers
  still receive `--no-subagents`.
- When a named provider is rate-limited, unavailable, or timed out,
  Firstchair keeps the finished stages and continues with the next named
  provider. One remaining provider can finish the run. `firstchair resume`
  continues an interrupted run, or a run stopped because every named
  provider was out of usage, from the last finished stage.
- `firstchair doctor` checks Codex, Claude Code, and Grok Build together,
  with at most three version probes in flight. `doctor --offline` still
  only searches PATH.
- Resume rejects a malformed exhausted seat, attempt count, or saved stage
  instead of retrying a spent provider or continuing with a missing plan.
  A stage failure is still written when usage details cannot be read.
- Grok Build runs are started with `--deny run_terminal_command` as well as
  the existing write and MCP denies. The installed Grok binary was not
  re-run for this change.

## 0.1.0a2

Fixes from the post-release review of the live adapters.

- Secret-looking variables, including test-prefixed ones, no longer reach
  live provider processes.
- Each provider executable is resolved once to an absolute path, so the
  version probe and every stage run the same file even with a relative
  `PATH` entry.
- The `--version` probe now has bounded output and process-group cleanup.
- Cleanup after a timeout or Ctrl-C no longer waits indefinitely on a
  descendant that left the process group but still holds stdout.
- A run whose failed stage reported no token counts now shows `partial`
  usage instead of `measured`.
- `--profile` and `--reviewer-profile` are validated before a dry run
  reports readiness.

## 0.1.0a1

- Added read-only live adapters for Codex CLI, Claude Code and Grok Build.
  `firstchair run --provider codex|claude|grok` runs plan, review and
  synthesis with your existing sign-in. `--reviewer` picks a different model
  for the review, `--workspace` lets providers read a directory, and
  `--format text` prints a readable summary. Each run records stage outputs
  and measured token usage where the provider reports it.
- Live providers run without write tools or MCP servers, never receive API key
  variables, and are killed with their process group on timeout or Ctrl-C.
- Accept verified provider versions and a bounded compatible range. Unknown
  versions are rejected before any run starts. `firstchair doctor` reports
  each provider's version and contract status.
- Claude Code rate-limit and authorization failures are reported as
  `rate_limited` and `authorization_failed`.
- Added `firstchair delegate` for dispatching Codex and Grok Build workers
  into Git worktrees, and `firstchair auth` for named provider sign-ins.
- Renamed the project from TriadMux to Firstchair. The distribution, import
  package and command are now `firstchair`, and the default state directory
  is `.firstchair/`. For one release, a `triadmux` command prints a notice
  and runs the same CLI, `.triadmux/` is read when `.firstchair/` is absent,
  and `TRIADMUX_TEST_` variables still reach provider children. Old run
  records stay valid.
- Added structured synthetic reviews, explicit usage provenance, provider
  readiness reporting, and dry-run routing explanations.
- Added synthetic request and workflow budgets with optional TOML configuration.
- Added run summaries, guarded POSIX recovery, and bundled worker cleanup after
  controller exit.
- Added offline Codex and Claude result parser tests and an installed-wheel
  workflow smoke check.
- Added offline Grok Build 1.0.77 streaming-json framing checks. Reply and
  usage acceptance stay disabled.
- Run each provider subprocess in a temporary workspace. A dry-run reports
  that workspace as `temporary`. Relative `PYTHONPATH` entries stay resolvable.
- Give provider children an explicit environment allowlist, and replace
  secret environment values in saved run records with `[redacted]`.
- Require an annotated `v` tag that matches the package version and changelog
  before publishing a GitHub Release.

## 0.0.0

- Added a credential-free leader, reviewer and synthesis demo with persisted run status.
- Added executable discovery for Codex, Claude Code and Grok Build.
- Established the MIT-licensed project, contributor workflow and release pipeline.
