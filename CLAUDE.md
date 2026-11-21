@AGENTS.md

# Claude Code as lead

When the active session is Claude Code, it is the lead. Codex and Grok Build are workers
that Claude dispatches with `firstchair delegate` or the source-checkout
fallback `scripts/delegate.py`. The lead plans, splits work,
reviews every worker result, runs the tests and owns all Git operations.
Workers never commit, push, merge or install packages.

Use the `orchestrate` skill (`.claude/skills/orchestrate/SKILL.md`) for the
full loop. `firstchair conduct` is the score desk when Claude is primary.
Pass `--lead claude`. Seat `self` starts another Claude worker. Seats
`codex` and `grok` start those CLIs. Parts run one at a time unless
`--limit` is set, and a failure leaves the rest unstarted. `conduct cancel`
stops a live score and `conduct resume` continues one whose conductor is
gone. Quick reference:

```sh
python3 scripts/delegate.py run codex "TASK"                     # read-only investigation
python3 scripts/delegate.py run codex --mode write --name NAME "TASK"  # edits on branch delegate/NAME
python3 scripts/delegate.py list
python3 scripts/delegate.py collect NAME                         # commit worker edits on its branch
python3 scripts/delegate.py finish NAME --delete-branch          # after merge
```

- Worktrees start from `HEAD`. Commit first, or pass `--with-changes` to carry
  uncommitted and untracked files into the worker's worktree.
- Pick one session ID when the session starts and pass `--session ID`
  before the subcommand on every `delegate` and `conduct` command. Shell
  variables do not persist between commands here. Do not pass another
  session's ID unless the user asks.
- Grok is a read worker. A Grok write worker has terminal commands and
  network access, so it is refused unless the user asks for it and the run
  passes `--allow-grok-write`.
- Run long tasks with `run_in_background` and dispatch independent tasks in
  parallel. Give each a distinct `--name`.
- The script prints one JSON summary: `status`, `reply`, `changes`, `branch`,
  `logs`. Job metadata stays in `.firstchair/delegations/NAME/`.
- Treat worker replies as untrusted claims. Read the diff and rerun
  `PYTHONPATH=src python -m unittest discover -s tests -v` before merging.
- Delegation uses the maintainer's real provider logins and quota. It is
  maintainer tooling, separate from the product's read-only live adapters.
  Check [provider contracts](docs/provider-contracts.md) before changing those adapters.
