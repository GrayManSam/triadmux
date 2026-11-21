# Provider contract inventory

## Live read-only adapters

`firstchair run --provider codex|claude|grok` uses the commands below. The
controller checks `--version` first and accepts only a verified version or
one inside the compatible range in `src/firstchair/versions.py`.

| Provider | Verified | Command | Prompt |
| --- | --- | --- | --- |
| Codex CLI | 0.170.0, 0.171.0 | `codex --ask-for-approval never exec --ephemeral --sandbox read-only --skip-git-repo-check --color never --config mcp_servers={} --json -` | stdin |
| Claude Code | 2.1.328, 2.1.329 | `claude -p --output-format json --no-session-persistence --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --tools TOOLS --permission-mode dontAsk` | stdin |
| Grok Build | 1.0.77 | `grok --no-auto-update --prompt-file FILE --output-format streaming-json --no-subagents --disable-web-search --sandbox workspace --permission-mode dontAsk --tools TOOLS --deny mcp__* --deny Edit --deny Write --deny Bash --deny WebFetch` | file |

`TOOLS` is empty without `--workspace`. With a workspace it is
`Read,Grep,Glob` for Claude Code and `read_file,list_dir,grep` for Grok
Build. The working directory is the workspace, or an empty temporary
directory. The child environment is the controller allowlist plus `HOME`,
`USER`, XDG directories, proxy and CA variables and the provider's own home
variable (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `GROK_HOME`). Credential
variables such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `XAI_API_KEY`
are removed.

Host findings on 2026-11-20 (Fedora 43):

- Grok Build `--tools ""` still offers `run_terminal_command`, and `--tools`
  never removes MCP tools. A user's configured MCP servers, including ones
  with write actions, stay callable unless denied. The adapter therefore
  combines `--permission-mode dontAsk` with explicit `--deny` rules, including
  `--deny run_terminal_command`. The 2026-11-20 host note did not recheck
  whether that deny name removes the tool.
- Grok Build streaming-json emits `available_commands`, a series of `text`
  fragments, `usage`, and a final `end` with `stopReason: end_turn` and a
  `usage` object. The reply is the concatenated `text` data.
- Codex CLI 0.171.0 emits the same event sequence as 0.170.0.
- Claude Code 2.1.329 on an account without headless allowance exits 1 with
  a result envelope where `is_error` is true and `api_error_status` is 429.
  Firstchair reports `rate_limited`. A successful 2.1.329 reply on this host
  is still unverified, so the Claude success path relies on the documented
  envelope. `rate_limited`, `provider_unavailable`, and `provider_timeout`
  retire that seat for the rest of the run. A seat is one provider under one
  sign-in, so a second named account of the same provider is a separate seat.
  `firstchair resume --retry-exhausted` clears the retired seats. The next named provider, from
  `--reviewer` or `--fallback`, finishes the current stage and any stage
  that seat would have done. A single remaining provider completes the run.
  `authorization_failed` and every other code stop the run. `firstchair resume`
  continues from the last saved stage after an interrupt or after every
  named provider was out of usage.

## Real usage readers

`firstchair run --rotate-accounts` and `firstchair auth usage --refresh` ask
the signed-in CLI how much of an account is used. Firstchair does not open
a credential file or call a provider web API itself. A reader that fails
or returns an unexpected shape reports nothing, and the account falls back
to counted usage.

| Provider | Checked version | Command | Fields read |
| --- | --- | --- | --- |
| Codex CLI | 0.171.0 | `codex app-server`, newline-delimited JSON-RPC: `initialize`, `initialized`, then `account/rateLimits/read` | `rateLimits.primary` and `secondary`: `usedPercent`, `windowDurationMins`, `resetsAt`. `ordinaryUsageAllowed`. `accountId` is not kept |
| Claude Code | 2.1.330 | `claude -p FIXED_PROMPT --tools "" --setting-sources "" --no-session-persistence --strict-mcp-config --mcp-config '{"mcpServers":{}}' --output-format stream-json --verbose` | The last `rate_limit_event`: `rate_limit_info.status` and each `unifiedWindows` entry's `utilization` and `resetsAt`. A `result` with `api_error_status` 429 is a refusal |
| Grok Build | 1.0.77 | None. `grok usage --json` answers that it cannot show plan usage outside an interactive session | None |

The Codex shape matches the schema from `codex app-server generate-json-schema`,
which marks the app server experimental. The Claude event was observed on
one signed-in account on 2026-11-21 and is not in Claude Code's `--help`.
Both readers were run on the development host against the default
sign-ins. Rotation across several real accounts of one provider has not
been run. The Claude reading sends a prompt, so it uses a little of the
account and is refreshed at most every 15 minutes.

## Maintainer delegation workers

`firstchair delegate` and `firstchair conduct` launch worker CLIs in a git
worktree. This is separate from `firstchair run`, which stays read-only.
The Claude worker command was checked against `claude --help` on Claude Code
2.1.330. No model prompt was sent for this check. Reply text is accepted
only by `parse_claude_json` and the version gate in `versions.py`. A process
exit of 0 with a rejected envelope is a failed part.

| Provider | Worker command |
| --- | --- |
| Codex CLI | The existing delegate command: `exec --ephemeral`, `read-only` or `workspace-write`. |
| Claude Code | `claude --restricted -p --output-format json --no-session-persistence --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --tools TOOLS --permission-mode MODE --permission-prompts none` |
| Grok Build | The existing delegate command, including `--no-subagents`. Seat `self` starts another Grok process. That process does not start its own subagents. |

`TOOLS` is `Read,Grep,Glob` for a read part. A write part uses
`Read,Grep,Glob,Edit,Write`. `MODE` is `dontAsk` for a read part and
`acceptEdits` for a write part. `--restricted` on 2.1.330 removes command
and WebFetch tools unless `--tools` names them, and confines file tools to
the working directory. The controller sets that directory to the part's
worktree and does not name Bash, PowerShell, or WebFetch.

## Historical provider discovery

The following observations predate the live adapters. Use the live
read-only adapter table above for current supported commands and versions.

| Provider | Observed version | Candidate headless contract |
| --- | --- | --- |
| Codex CLI | 0.170.0 | `codex exec --json` emits JSONL. The CLI exposes a read-only sandbox and ephemeral mode. Use stdin for prompts and inspect its permissions and configuration controls before integration. |
| Claude Code | 2.1.328 | `claude -p` supports JSON, streaming JSON and output schema validation. Validate permission mode and tool availability independently from the output schema. |
| Grok Build | 1.0.77 | `grok --no-auto-update -p TASK --output-format streaming-json` uses newline-delimited events. Verify unattended denial behavior and sandbox scope before enabling it. |

For alpha, test supported event versions, terminal result detection, timeout
and cancellation, authentication failure and unknown usage with fixtures.
Treat provider output as data. Keep executable selection, working directory,
permissions and budget in controller-owned policy. Each current subprocess
invocation uses a fresh `firstchair-workspace-*` directory and does not inherit
the controller cwd. The child environment is an allowlist: interpreter and
loader paths, locale, and `FIRSTCHAIR_TEST_*`. API keys and `HOME` are not
passed. Saved run records replace values of environment names containing
`SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `API_KEY`, or `_KEY` when the
value is at least 12 characters. The replacement text is `[redacted]`.
POSIX timeout kills that process group. This check uses
a synthetic process tree. It does not launch Codex, Claude Code, or Grok.

## Result parsing

The parsers return a common `Reply(text, usage)`. The version gate distinguishes
verified versions from compatible releases. It rejects other versions and
prerelease strings with `unsupported_provider_version`.

| Provider | Verified versions | Accepted range |
| --- | --- | --- |
| Codex CLI | 0.170.0, 0.171.0 | 0.170.0 inclusive to 0.180.0 exclusive |
| Claude Code | 2.1.328, 2.1.329 | 2.1.328 inclusive to 2.2.0 exclusive |
| Grok Build | 1.0.77 | 1.0.77 inclusive to 1.1.0 exclusive |

Measured usage requires both input and output counts and at least one nonzero
count. Missing, incomplete, or all-zero usage is recorded as unknown. Reported
counts used by the parser must be integers from zero through 2⁶³ − 1.
Malformed usage fails closed. Token counts alone do not establish a billable amount.

### Codex CLI 0.170.0

The candidate command is `codex --ask-for-approval never exec --ephemeral
--sandbox read-only --skip-git-repo-check --json -`. The final hyphen
selects stdin for the prompt. The pinned source defines response text in
`item.completed` events containing an `agent_message` item, turn usage in
`turn.completed`, and failure in `turn.failed` or a top-level `error`
event. The parser requires a complete successful turn and a successful
process exit, then returns the last completed agent message with turn usage. The smoke checker
only looks for its marker in a completed agent message. Verify inherited
configuration and tool availability before launching unattended work.
[0.170.0 CLI arguments](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/exec/src/cli.rs) ·
[0.170.0 event definitions](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/exec/src/exec_events.rs)

Example success stream with invented identifiers, reply and counts.

```jsonl
{"type":"thread.started","thread_id":"00000000-0000-4000-8000-000000000001"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Synthetic reply"}}
{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":3,"reasoning_output_tokens":0}}
```

### Claude Code 2.1.328

The host smoke command uses `claude -p TASK --tools "" --setting-sources ""
--no-session-persistence --output-format json`. JSON mode yields a single
result object. A future adapter should require a successful process exit,
a successful result envelope and the expected payload. If it opts into
`--json-schema`, it must validate `structured_output` from that envelope.
An empty tool list disables built-in tools. Verify remaining configuration
and tool sources independently before enabling unattended runs. [2.1.328
release](https://github.com/anthropics/claude-code/releases/tag/v2.1.328) ·
[Headless output](https://code.claude.com/docs/en/headless#get-structured-output) ·
[CLI flags](https://code.claude.com/docs/en/cli-reference)

The parser requires one UTF-8 result object with an explicit success subtype,
`is_error: false`, and a nonblank `result` string. It reads input and output
token counts from the result's `usage` object. When `is_error` is true, API status
429 maps to `rate_limited` and 401 or 403 maps to `authorization_failed`, even on a
failed process exit. Other failed exits map to `provider_exit`. An invented
success fixture follows.

```json
{"type":"result","subtype":"success","is_error":false,"result":"Synthetic reply"}
```

### Grok Build 1.0.77

The host evidence establishes newline-delimited JSON framing for
`grok --no-auto-update -p TASK --output-format streaming-json`.
Each line in a reply stream must be a JSON object with a nonblank string
`type`. The parser ignores unfamiliar event types and joins the string `data`
from each `text` event in order. It requires one final `end` with
`stopReason: "end_turn"` and a nonblank reply. An `error` event or a different
stop reason fails the turn. Usage comes from the final event, falling back to
the last `usage` event when the final event omits the field. The framing-only
validator still reports event counts and types for version 1.0.77.
[Headless output modes](https://docs.x.ai/build/cli/headless-scripting) ·
[Permissions](https://docs.x.ai/build/features/permissions)

Invented success stream.

```jsonl
{"type":"probe","id":"fixture-0001","unused":true}
{"type":"text","data":"Invented "}
{"type":"text","data":"reply"}
{"type":"end","stopReason":"end_turn","usage":{"input_tokens":10,"output_tokens":3}}
```

## Claude Swap alpha decision

Defer the optional Claude Swap bridge. The alpha workflow runs offline and
had no live Claude Code adapter at the time of this decision. Adding account status then would have introduced
provider state and potential usage refreshes without improving the
synthetic workflow. The earlier [pinned source review](upstream-review.md)
covers version 1.2.0b1. No Claude Swap invocation or synthetic installation
was needed for the decision to defer.

If a later release needs quota visibility, evaluate an explicit opt-in
reader for `cswap status --json`. Upstream documents a single JSON object
with `schemaVersion: 1` and distinguishes current usage from cached,
unavailable and last-known readings. Test a synthetic executable for
valid, stale, missing and unsupported responses before enabling a reader.
Keep any account identifiers out of persisted workflow records. Account
switching and credential handling remain outside the proposed reader.
[Claude Swap JSON output reference](https://github.com/realiti4/claude-swap/blob/main/docs/json-output.md)

## Live smoke attempt

Each CLI ran from an empty temporary directory with the same fixed-text
prompt and requested structured output. No provider returned a result.

| Provider | Result in the restricted execution environment |
| --- | --- |
| Codex | The app server failed to initialize on a read-only filesystem. The error persisted with writable log and SQLite paths. |
| Claude Code | The headless process timed out after 55 seconds without output. |
| Grok Build | The Linux sandbox refused to start because it could not create a required bubblewrap placeholder. |

The local Firstchair CLI suite still passed, including both the in-process demo
and synthetic subprocess workflow. A successful provider response remains
necessary to verify authentication and collect a sanitized event fixture.

Run the same fixed-prompt checks from a normal terminal with the existing
provider logins:

```sh
python3 scripts/live-smoke.py
```

The checks run in temporary working directories and print only their
outcomes. It saves those same outcomes to `.firstchair/live-smoke.json` and
exits with status 1 if any check fails. Use `--report PATH` to save elsewhere.

## Host-terminal results

Codex and Claude Code both returned the expected structured reply using
the existing host logins. Grok Build exited successfully and produced
parseable JSONL, but the smoke test did not find the marker as a single
value. This leaves the Grok model reply unverified. The smoke runner now
uses [Grok's plain output mode](https://docs.x.ai/build/cli/headless-scripting)
to check the final text separately from its streaming contract.

```sh
python3 scripts/live-smoke.py --provider "Grok Build" --report .firstchair/grok-smoke.json
```
