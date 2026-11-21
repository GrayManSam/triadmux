# Lead a Firstchair development session

The active agent session plans the task, dispatches independent pieces when
requested, reviews the results, and integrates verified changes. Use the
`--lead` value matching the session that invoked the dispatcher.

| Lead | Automatic read worker | Automatic write worker |
| --- | --- | --- |
| Claude Code | Grok Build, then Codex | Codex |
| Codex | Grok Build, then Claude Code | Claude Code |
| Grok Build | Codex, then Claude Code | Codex, then Claude Code |

`auto` does not select the lead. `self` is the explicit request for the
lead's own provider.

### Grok write workers need an explicit flag

A Codex write worker runs in Codex's `workspace-write` sandbox. A Claude
write worker gets file tools only. A Grok write worker is started with
`--permission-mode bypassPermissions` and `--sandbox workspace`. It can run
terminal commands and reach the network, so the rule that workers do not
push or merge is only an instruction to it. Firstchair therefore refuses a
Grok write worker before a worktree is created. This covers seat `grok`
and seat `self` under a Grok lead, in `delegate run` and in every `conduct`
command. `auto` never selects Grok for a write task. Pass
`--allow-grok-write` to accept the exposure for one invocation.

A Grok read worker uses the same permission mode with its two edit tools
removed. It can still run terminal commands and reach the network. The
installed Grok CLI was not re-run for this change, and the read command is
unchanged.

Use `firstchair delegate run auto --lead codex --dry-run` to preview worker
selection and timeout without starting a worker or creating a worktree.
`firstchair delegate --repo PATH` selects a checkout from another directory.
The repository's `python3 scripts/delegate.py` entry point remains available.

Automatic selection checks PATH before dispatch. An invocation makes one worker
attempt. The summary records the requested worker, chosen worker, and lead.

```sh
firstchair delegate run auto --lead codex --name parser-review --prompt-file /tmp/review.md
firstchair delegate run auto --lead grok --mode write --name parser-fix --prompt-file /tmp/fix.md
firstchair delegate run codex --lead claude "Review the parser tests"
```

## Session IDs

Several lead sessions can share one checkout. Each score and each delegate
job records the ID of the lead session that started it, so one session does
not cancel, resume, collect, or finish another session's work by mistake.

Pick one ID when a lead session starts and use it for every command in
that session. Pass `--session ID` before the subcommand, or set
`FIRSTCHAIR_SESSION`. The flag wins. An ID is 1 to 63 characters of `a-z`,
`0-9`, `.`, `_`, or `-`. When neither is set, `delegate run` and
`conduct run` make an ID such as `s-3f9a1c0b7d2e` and print it as
`session` in their JSON.

```sh
firstchair conduct --session claude-parser run --lead claude --name slice --score score.json
firstchair conduct --session claude-parser cancel slice
firstchair delegate --session claude-parser finish parser --delete-branch
```

- `conduct cancel`, `conduct resume`, `delegate collect`, and
  `delegate finish` are refused unless the session matches the recorded
  one. A command with no session is refused too. The error names the
  owning session.
- `conduct status` and `delegate list` are read-only. They work from any
  session and show each owner.
- The parts of a score carry the score's session.
- The ID is saved on the board, in `.firstchair/delegations/NAME/session`,
  and in the job summary.
- A job or score recorded before session IDs existed has no owner and can
  be acted on from any session.

The ID prevents mix-ups. It is not a secret and does not control access.
Any session can read another's ID and pass it on purpose. Score names, job
names, worktrees, and branches are still shared across sessions, so a name
already in use is refused.

`firstchair run` records the session too. Pass `--session ID` before the
command. `firstchair resume` and `firstchair recover` are refused from a
different session. `firstchair status` and `firstchair runs` are read-only
and show the owner.

## Conductor score

The primary seat runs the score. Pass `--lead` as that seat. Each part names
a seat: `self` starts the lead's own provider as a separate worker, `auto`
uses the table above, and `codex`, `claude`, or `grok` starts that CLI.
A Grok worker still receives `--no-subagents`. The conductor is what fans
the work out. The worker does not fan out again.

Parts run one at a time by default. The next part does not start after a
failure. Write parts keep branch `delegate/NAME`. Read parts remove their
worktrees. The board at `.firstchair/conduct/SCORE/board.json` stores the
limit and each part's status, seat, provider, and exit code. It does not
store the task or the reply. The reply is in the JSON printed when the
score returns.

`--limit N` runs up to N parts at once, where N is 1 through 4. A score can
set the same value as a top-level `"limit"`. The flag overrides the score.
Any other value is refused before a worker starts or a board is written.
Parts run in waves of N in input order, and a wave ends when all of its
parts end. After a wave with a failed part, no later wave starts. Parts
already running in that wave finish. Parts that never started are
`not_started`. The printed summary and the board keep input order, and the
exit code comes from the earliest failed part in that order. Only give
parts that do not depend on each other a limit above 1.

A part can read the reply of one earlier part. Set `"after"` to that part's
name. The conductor adds the earlier reply to the follow-up prompt, marks it
as untrusted output from another worker, and cuts it at 32,000 characters.
The reply stays in the conductor's memory. The board stores the `after`
name and not the reply. The earlier part must finish before the follow-up
starts, so with a limit above 1 the two must fall in different waves. A
score that breaks this is refused before a worker starts. A follow-up fails
without starting a worker when the earlier part returned an empty reply.

```json
{
  "parts": [
    {"name": "own-notes", "seat": "self", "mode": "read", "task": "Summarize the backlog"},
    {"name": "parser", "seat": "codex", "mode": "write", "task": "Add the regression test"},
    {"name": "contract", "seat": "claude", "mode": "read", "task": "Check the worker flags"},
    {"name": "second-look", "seat": "grok", "mode": "read", "after": "contract",
     "task": "Check the findings in the earlier reply against the source"}
  ]
}
```

```sh
firstchair conduct plan --lead grok --score score.json
firstchair conduct run --lead grok --name rehearsal --score score.json
firstchair conduct run --lead grok --name together --score score.json --limit 3
firstchair conduct status rehearsal
firstchair conduct cancel rehearsal
firstchair conduct resume --name rehearsal --score score.json
```

### Tell a slow part from a dead conductor

Each part on the board records `started_at` and `ended_at` in UTC. While
the board says `running`, `firstchair conduct status NAME` also prints two
values that are not saved. `conductor` is `live` when a process holds the
score lock and `gone` when none does. `elapsed_seconds` on a running part
can be compared with its `timeout_seconds`. A `live` conductor with a part
inside its timeout is still working. Grok reviews often take several
minutes. A `gone` conductor will never finish the score. Use `resume`.

### Cancel a running score

Run `firstchair conduct cancel NAME` from another terminal. The conductor
checks for the request about ten times a second. It kills the process group
of each running part and starts no other part. A cancelled read part's
worktree is removed. A cancelled write part keeps its worktree and branch
for review. The board status becomes `cancelled`, the running parts are
`cancelled`, later parts are `not_started`, and the conductor exits 2. A
part that completed before the request stays `completed`.

Cancel is refused when the board is not `running`. It is also refused when
the board says `running` and no conductor holds the score lock. That score's
conductor died, and `resume` is the next step.

### Resume a score

`firstchair conduct resume --name NAME --score FILE` continues a score that
is not completed and has no live conductor. The conductor holds a lock on
`.firstchair/conduct/NAME/lock` while it runs. The operating system releases
the lock when the process ends, including when it is killed. Resume takes
that lock first and is refused while another conductor holds it. Resume
requires POSIX file locks.

The board has no task text, so pass the score file again. Its parts must
match the saved names, seats, modes, and `after` names in order. The lead
comes from the board. Task text is not compared.

- A completed part is not run again, with one exception below.
- A read part that did not complete runs again. Its leftover worktree and
  log directory are removed first.
- A write part that did not complete and left a worktree or branch is
  refused, and nothing is changed. Review the worktree. Then run
  `firstchair delegate finish NAME --delete-branch`, with `--force` to
  discard its edits, and resume again.
- Replies are not saved. When an unfinished follow-up part needs the reply
  of a completed read part, that read part runs again first. This uses the
  provider again for work that had finished.
- A completed write part is never replayed. A follow-up of a completed
  write part is refused. Start a new score for the remaining parts.

A worker process whose conductor was killed can keep running in its own
process group until its provider exits. Check for it before resuming a
score whose conductor was killed.

A score holds at most eight parts. Claude workers run with `--restricted`.
Read parts get Read, Grep, and Glob. Write parts also get Edit and Write,
under `acceptEdits`. Command tools are not named. `firstchair run` stays the
read-only product path and does not use this desk.

Pick one testable slice from ROADMAP.md and TODO.md. Give each worker a distinct
name, files in scope, and acceptance criteria. Independent tasks can run
concurrently in separate worktrees. Worktrees start from HEAD. Review pending
changes before intentionally carrying them into a worker with `--with-changes`.

Read the JSON returned by the command and inspect each write worktree's diff.
The dispatcher saves job metadata locally and removes temporary prompt and
provider output files when the invocation ends. Run
`PYTHONPATH=src python -m unittest discover -s tests -v` before integrating.
The lead owns Git integration.

Delegation uses the worker CLI's existing login and provider quota. Select an
explicit worker to pass `--model` or `--effort`. Summaries report measured
input and output tokens for a successful Codex run, and for a Claude worker
reply the versioned parser accepts. Other usage is reported as unknown.
Automatic account selection, quota-based retries, and model optimization
require provider contracts and policy beyond the dispatcher's PATH check.

## Pick a worker's account from real usage

Add `--rotate-accounts` to `firstchair delegate run` or to `firstchair
conduct run` and `resume`. The worker then runs under the account of its
provider with the most room: the default sign-in or one of the named ones.
The choice uses the same real usage readings, switch point, and rests as
`firstchair run --rotate-accounts`, with the default rules. `--dry-run`
shows the candidates. The summary and the board record the chosen account
as `codex` for the default sign-in or `codex:NAME` for a named one. When
every account of the provider is full or resting, nothing starts.

A completed worker is counted against its account in the ledger. A failed
worker is not, because the dispatcher does not know why it failed. Parts
that run at the same time can pick the same account. `--rotate-accounts`
cannot be combined with `--profile`.

## Local verification

The installed dispatcher completed fixed-prompt read jobs through Codex and
Grok Build in temporary Git repositories on the development host. The same
jobs failed to start under the restricted workspace sandbox. Codex hit a
read-only filesystem and Grok Build's sandbox could not initialize. Claude
Code's separate fixed-prompt probe exited with code 1. Claude Code can lead
this workflow by selecting a Codex or Grok Build worker. That smoke did not
start a Claude worker. Conductor coverage for a Claude worker is synthetic.

## Sign in and choose accounts

On POSIX, use a distinct alias for each account. Run login from a terminal
and complete the provider's native browser flow. Over SSH, add
`--device-auth` for Codex and Grok and open the printed URL on your local
device. For Claude, follow the native sign-in URL and paste the browser code
into the SSH terminal if prompted. Treat sign-in URLs and codes as private.

```sh
firstchair auth login codex work
firstchair auth login codex personal
firstchair auth login claude work
firstchair auth login grok work
firstchair auth list
firstchair auth check codex work
firstchair auth check claude work --probe
firstchair delegate run codex --profile work "Review the parser tests"
```

The account directories live under
`${XDG_STATE_HOME:-$HOME/.local/state}/firstchair/accounts`. Firstchair
requires private, user-owned directories outside Git checkouts and starts
named login and worker processes with a restrictive umask. Providers manage
credential storage. File-backed provider storage depends on the permissions
of the local account and filesystem for confidentiality. Firstchair stores
no credentials or provider account identifiers in its job metadata.

Named sessions set the provider home for that process. Claude login uses
claude.ai, whose per-directory behavior is documented by Anthropic. Keyless
Claude Console profiles use a separate native authentication system. Codex
and Grok named profiles can be selected for delegation. Claude profiles
currently support native login, status, and a fixed-prompt inference probe.

`auth check` without an alias examines the current native login. Grok's
check sends a small fixed prompt because its CLI has no status subcommand.
Claude's `--probe` sends a fixed prompt and reports `rate_limited` for
HTTP 429. A signed-in Claude account can still be rate limited. Retry after
the provider's allowance resets.

[Codex authentication](https://learn.chatgpt.com/docs/auth) ·
[Claude Code authentication](https://code.claude.com/docs/en/authentication) ·
[Grok Build settings](https://docs.x.ai/build/settings) ·
[Grok Build sign-in](https://docs.x.ai/build/overview)
