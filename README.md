# Firstchair

**Make Codex, Claude Code and Grok check each other's work.**

Firstchair is a local-first command-line tool for people who already use
more than one AI coding assistant. One model leads. A second model reviews
its plan. The lead then writes the final answer with that review in hand.
Firstchair drives Codex CLI, Claude Code and Grok Build through their own
headless modes and your existing sign-ins, runs them read-only, and keeps
a record of every run on your machine.

```sh
firstchair run --provider codex --reviewer claude --workspace . --format text \
  "Find the riskiest error handling in src/ and propose fixes"
```

## Why use it

- **A second opinion on every answer.** A different model reviews the plan
  before the final answer is written.
- **Your tools, your logins.** No new service and no API keys. Each
  assistant uses its own native sign-in.
- **Read-only by default.** `firstchair run` cannot edit your files or call
  your MCP servers.
- **Keeps going when a provider runs out of usage.** The run moves to the
  next provider or account you named and keeps the finished stages.
- **Switches accounts on real usage.** Firstchair reads the percent used
  from Codex and Claude Code and moves to the account with the most room
  before a limit is hit.
- **Everything stays local.** Run records live on your machine, with
  secret environment values redacted.
- **Multi-agent development for maintainers.** `firstchair conduct` runs
  several workers in isolated Git worktrees, with cancel and resume.

Firstchair is alpha software (`0.1.0a2`). Interfaces can change before 1.0.

## Contents

- [Quick start](#quick-start)
- [How a run works](#how-a-run-works)
- [When a provider runs out of usage](#when-a-provider-runs-out-of-usage)
- [Safety and privacy](#safety-and-privacy)
- [Session IDs](#session-ids)
- [Multi-agent development](#multi-agent-development)
- [Offline demo](#offline-demo)
- [Project status](#project-status)

## Quick start

You need Python 3.12 or later and at least one of Codex CLI, Claude Code or
Grok Build, installed and signed in.

```sh
pipx install git+https://github.com/GrayManSam/firstchair   # or: pip install -e . from a checkout
firstchair doctor
firstchair run --provider codex --format text "Explain what this project's tests cover"
firstchair runs
firstchair status <run-id>
```

`doctor` finds the Codex, Claude Code and Grok executables on PATH, runs
each one's `--version`, and reports whether that version is verified,
compatible or unsupported. `doctor --offline` only searches PATH.

No assistant installed yet? Try the [offline demo](#offline-demo).

## How a run works

`firstchair run --provider PROVIDER TASK` runs three stages:

1. **Plan.** The lead provider plans the task.
2. **Review.** The reviewer critiques the plan. It defaults to the lead.
   Pass `--reviewer` to get a second model's opinion.
3. **Synthesize.** The lead writes the final answer from its plan and the
   review.

| Option | Effect |
| --- | --- |
| `--reviewer codex\|claude\|grok` | Provider for the review stage |
| `--fallback PROVIDER[:PROFILE]` | Extra provider, or a second named account, used when an earlier one is out of usage. Repeat once for a third |
| `--workspace DIR` | Directory providers may read. Without it they run in an empty temporary directory |
| `--profile NAME`, `--reviewer-profile NAME` | Use a named sign-in from `firstchair auth login` |
| `--timeout SECONDS` | Per-stage limit (default 600) |
| `--total-timeout SECONDS` | Budget across all stages |
| `--format text` | Print the plan, review and result instead of the JSON record |
| `--dry-run` | Show roles, versions and limits without starting a model |
| `TASK` = `-` | Read the task from stdin |

Each run record keeps every stage's text, provider version and token usage.
Usage is `measured` when the provider reports counts, `partial` when only
some stages did, and `unknown` otherwise. Token counts are not a bill.
Unknown provider versions are refused before a run starts.

## When a provider runs out of usage

A named provider that is out of usage (`rate_limited`), unavailable, or
timed out is skipped. Firstchair keeps the stages already finished and
gives the rest of the run to the next seat you named, so two providers can
become one and still finish. A login or authorization failure stops the run
instead of switching.

A seat is one provider under one sign-in. That means a second account of
the same provider is its own seat and can take over:

```sh
firstchair auth login claude work
firstchair auth login claude personal
firstchair run --provider claude --profile work --fallback claude:personal \
  --fallback codex "Review the parser"
```

Firstchair only uses accounts you signed in and named yourself. Check that
your provider's terms allow the way you use more than one account.

### Rotate across many accounts

With `--rotate-accounts`, every named sign-in of the providers in the run
joins a pool, up to 100 accounts. Before each stage Firstchair reads each
account's real remaining usage, picks the account with the most room, and
moves on before a provider refuses the work.

```sh
firstchair run --provider codex --reviewer claude --rotate-accounts "Review the parser"
firstchair auth usage --refresh
```

Where the real numbers come from:

| Provider | Real usage | How it is read |
| --- | --- | --- |
| Codex CLI | Percent used and reset time of the 5-hour and weekly windows | `codex app-server`, request `account/rateLimits/read`. No prompt is sent |
| Claude Code | Utilization and reset time of the 5-hour and 7-day windows | The `rate_limit_event` Claude Code prints in `stream-json` mode. One short fixed prompt per reading, kept for 15 minutes |
| Grok Build | Not available | `grok usage` only works inside an interactive Grok session. Firstchair counts its own calls and reacts to refusals |

Firstchair asks the signed-in CLI. It does not open a credential file or
call a provider's web API itself.

`firstchair delegate run --rotate-accounts` and `firstchair conduct run
--rotate-accounts` pick each worker's account the same way, from the
provider's default sign-in and its named ones.

| Option | Effect |
| --- | --- |
| `--account-switch-percent N` | Move off an account when a real usage window reaches N percent (default 90) |
| `--account-token-limit N` | Also move off after N tokens sent by Firstchair in the window |
| `--account-call-limit N` | Also move off after N stages in the window. Use it for Grok |
| `--account-window SECONDS` | How far back the two counted limits look (default 18000, five hours) |
| `--account-cooldown SECONDS` | How long an account rests after a usage failure with no reset time (default 3600) |

How an account is chosen for a stage:

1. An account is skipped while the provider reports a window full or
   refuses it, and while it rests after a usage failure.
2. Accounts under the switch point come before accounts at or past it. An
   account past it still runs the stage when it is all that is left.
3. Accounts of the provider that holds the role come first.
4. The account the run is already using stays while it is under the
   switch point.
5. Otherwise the account with the lowest real percent used goes first.

Readings, counts and rests are kept in one ledger for your user, shared by
every checkout. It holds no task text and no account identifier beyond the
profile name you chose. Each run record lists the accounts considered for
every stage, their usage, and which one was picked. `firstchair auth usage`
shows the ledger, and `firstchair auth clear-cooldown [ACCOUNT ...]` ends a
rest early.

`firstchair resume RUN_ID` continues an interrupted run, or a run that
stopped because every named seat was out of usage, from the last finished
stage. A resumed run keeps the same lead and reviewer.

- Add `--provider` or `--fallback` on resume to hand the rest to a seat
  that still has usage.
- Add `--retry-exhausted` to try the skipped seats again, for example
  after a limit resets.

## Safety and privacy

- **Read-only providers.** Codex uses its `read-only` sandbox. Claude Code
  gets only `Read`, `Grep` and `Glob`. Grok Build gets only `read_file`,
  `list_dir` and `grep` with deny-by-default permissions.
- **No MCP servers.** They are switched off for all three during a run.
- **No API keys passed along.** Provider processes receive an allowlisted
  environment, so each one uses its own native sign-in.
- **Clean stops.** On timeout or Ctrl-C the provider's whole process group
  is killed and the run is recorded as failed or interrupted.
- **Local records.** Run records can contain your task and result. They
  live in the ignored `.firstchair/` directory by default. Use
  `firstchair --state-dir PATH` to choose another location. Before a record
  is stored, secret environment values of at least 12 characters are
  replaced with `[redacted]`.

Report security problems through the [security policy](SECURITY.md).

## Session IDs

Several sessions can share one checkout. Each run, score and delegate job
records the session that started it, so one session does not resume,
cancel or finish another session's work by mistake.

```sh
firstchair --session review-a run --provider codex "Review the parser"
firstchair --session review-a resume <run-id>
```

Pass `--session ID` before the command or set `FIRSTCHAIR_SESSION`. When
neither is set, a command that starts work makes an ID and prints it.
`resume` and `recover` are refused from a different session. `status` and
`runs` work from any session and show the owner. The ID prevents mix-ups.
It is not a secret and does not control access.

## Multi-agent development

Maintainers can lead development from Claude Code, Codex, or Grok Build.
The primary seat is the conductor.

- `firstchair conduct` runs a score of up to eight parts. It can start the
  lead's own provider and the other two, run up to four parts at once,
  pass one part's reply to a follow-up part, and be cancelled or resumed.
- `firstchair delegate` runs one bounded task.

Each worker gets a Git worktree outside the checkout. Read tasks use a
disposable worktree. Write tasks keep branch `delegate/NAME` for review.
Workers do not commit. The lead reviews and integrates.

```sh
firstchair delegate run auto --lead codex --dry-run
firstchair delegate run auto --lead codex --name parser-review "Review the parser tests"
firstchair conduct plan --lead grok --score score.json
firstchair conduct run --lead grok --name rehearsal --score score.json --limit 2
firstchair conduct status rehearsal
firstchair delegate list
```

Unlike `firstchair run`, write-mode delegation lets a worker edit files
inside its worktree. A Grok write worker can also run terminal commands
with network access, so it is refused unless you pass `--allow-grok-write`.
Delegation uses your existing provider logins and quota. See
[the orchestration guide](docs/orchestration.md) for scores, cancel,
resume, sessions and account selection.

Use `firstchair auth login PROVIDER PROFILE` to sign in through the provider
CLI, and `firstchair auth list` to see local aliases.

Grok loads `AGENTS.md` and `.grok/` only in a trusted folder. Run
`grok --trust` once in the checkout. Worktrees of a trusted checkout
inherit it. Grok discovers agents under `.grok/`, the `firstchair` plugin,
and that plugin's `provider-fixture` skill. Those files stay in the
repository for offline fixture and parser work.

## Offline demo

The demo and bundled synthetic provider need no sign-in:

```sh
firstchair run --demo "Review the project's build instructions"
```

Run records can contain your task and result. Before a
record is stored, secret environment values of at least 12 characters are
replaced with `[redacted]`. Provider subprocesses receive an allowlisted
environment and do not inherit API keys. Records live in the ignored
`.firstchair/` directory by default. Use `firstchair --state-dir PATH`
to select another location.

The bundled synthetic subprocess adapter simulates a read-only handoff. Run
it by explicitly choosing `--provider synthetic` and passing a task. On success,
the run record includes the worker's structured review and the synthesis
result. Offline runs record token usage as unknown with null counts.

```sh
firstchair run --provider synthetic --dry-run --timeout 5 --total-timeout 10 "Review the build"
firstchair run --provider synthetic --timeout 5 --total-timeout 10 "Review the build"
```

`--dry-run` explains provider selection and shows the stage limits without
starting workers or creating a run record. For `--provider synthetic` it
also reports `"workspace": "temporary"`: each stage runs in a controller
temporary directory. `--max-input-bytes` applies to each
UTF-8 JSON request, including the envelope. Oversized requests fail with
`input_limit` before worker startup. Each worker also has a stdout byte limit
and a timeout. `--total-timeout` sets a shared budget across stages and reports
`budget_exhausted` when it expires. Cleanup can add time before the command
returns.

Pass `--config limits.toml` to reuse synthetic limits. Explicit flags take
precedence over the file, and omitted values use the bundled defaults. Select
the run mode on the command line.

```toml
[synthetic]
timeout = 5
total_timeout = 10
max_input_bytes = 65536
max_output_bytes = 65536
```

Use `firstchair runs` to find saved runs by ID, status, and stage. On POSIX,
`firstchair recover <run-id>` marks an abandoned run as interrupted once the
controller releases its run lock. Recovery updates the record only.
It requires a lock file created by the original run. Keep each run's
`.lock` file alongside its JSON record. A lifetime pipe stops bundled POSIX
workers when their controller dies.

A live provider that is killed with SIGKILL together with its controller is
not stopped by the lifetime pipe. Use `firstchair recover` to mark that run.

## Project status

Firstchair is in alpha. See [the roadmap](ROADMAP.md) for the alpha, beta
and release gates, [the to-do list](TODO.md) for the next slice, and
[the changelog](CHANGELOG.md) for what changed.

CI is configured for Ubuntu 24.04 with Python 3.12–3.14 and macOS with Python
3.12. The release workflow installs the wheel on Ubuntu with Python 3.12
and compares a completed synthetic workflow with its saved status. A public
release is an annotated tag named `v` plus the package version. The workflow
publishes that changelog section and rejects every other tag.

Run the tests with `PYTHONPATH=src python -m unittest discover -s tests -v`.
Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

Formerly TriadMux. The `triadmux` command still works for one release and
prints a notice. An existing `.triadmux/` state directory is read when
`.firstchair/` does not exist.

Firstchair is independently maintained and released under the
[MIT license](LICENSE). It is not affiliated with OpenAI, Anthropic or xAI.
See the [provider contract inventory](docs/provider-contracts.md) for the
exact commands each adapter runs and the [upstream review](docs/upstream-review.md)
for prior art.
