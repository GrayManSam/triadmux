---
name: orchestrate
description: Lead a Firstchair slice by dispatching Codex and Grok Build workers with scripts/delegate.py, then reviewing, merging and verifying their work. Use when work can be split, needs a second model's review, or the user asks Claude to drive Codex or Grok.
---

# Orchestrate Codex and Grok

Claude Code is the lead. Workers run headless through `scripts/delegate.py`,
each in its own git worktree. Use the `worktree` path reported by
`firstchair delegate list` for the named job.

## 1. Plan

1. Read `ROADMAP.md` and `TODO.md`. Pick one testable slice.
2. Split it into tasks that touch disjoint files. Write each task so a worker
   with no conversation context can finish it: goal, files in scope, files
   out of scope, acceptance criteria and the test command.
3. Choose a worker per task:
   - **Codex**: precise code edits, parsers, tests, code review of a diff.
   - **Grok**: broad reading and analysis, docs, second-opinion reviews.
   - **Claude (self)**: cross-cutting design, anything touching
     `provider.py`/`workflow.py` process and state handling, final synthesis.
4. Commit the lead's own pending work before dispatching, or pass
   `--with-changes` so workers see it.

## Conductor score

For a recorded desk, write a score JSON file and run it as the primary:

```sh
firstchair conduct plan --lead claude --score /tmp/score.json
firstchair conduct run --lead claude --name slice --score /tmp/score.json
firstchair conduct status slice
```

Seat `self` starts another Claude Code worker. Seats `codex` and `grok`
start those CLIs. The score runs one part at a time and stops after a
failure. The board does not store task text or replies. Independent
overlapping work still uses separate `delegate run` commands below until
the parallel score story in `TODO.md` is implemented.

## 2. Dispatch

```sh
# read: disposable detached worktree, removed afterwards
python3 scripts/delegate.py run codex --name review-x "Review ... Report file:line findings."
# write: worktree kept on branch delegate/NAME
python3 scripts/delegate.py run codex --mode write --name docs-x --prompt-file /path/task.md
```

- Run each dispatch with Bash `run_in_background: true` and launch independent
  tasks in the same turn. Defaults: 600 s read, 1800 s write. Override with `--timeout`.
- Options: `--model`, `--effort`, `--keep` (keep a read worktree), `--with-changes`.
- Exit codes: 0 completed, 1 worker failed, 124 timeout, 2 usage error.

## 3. Review

1. Read the JSON summary: `reply`, `changes.status`, `changes.diffstat`.
2. Inspect the real diff: `git -C <worktree> diff HEAD` plus untracked files.
   Do not trust the reply's claims about tests; rerun them in the worktree:
   `cd <worktree> && PYTHONPATH=src python -m unittest discover -s tests -v`.
3. Check AGENTS.md rules: invented fixtures only, no credentials or
   transcripts and fail-closed parsing. The product's live adapters are read-only.
4. For a non-trivial diff, get a cross-review: dispatch the *other* worker in
   read mode with the diff in the prompt, or review it yourself.
5. To request fixes, dispatch a follow-up write task with a new `--name` and
   `--with-changes` from inside that worktree (`--repo <worktree>`), or fix it yourself.

## 4. Integrate

```sh
python3 scripts/delegate.py collect NAME          # commits worker edits on delegate/NAME
git diff HEAD...delegate/NAME                      # final look
git merge --squash delegate/NAME && git commit     # lead writes the real commit message
python3 scripts/delegate.py finish NAME --delete-branch --force
```

Run the full test suite on the main checkout after merging, update `TODO.md`,
and record verification evidence. Commit or push only when the user asks.

## Guardrails

- One task per `--name`; never reuse a name while its worktree exists.
- Never paste `.firstchair/` contents, account data or tokens into a task.
- Each dispatch spends the maintainer's provider quota. Prefer one
  well-scoped task over many small ones.
- Grok's `read-only` sandbox needs bubblewrap, which fails on this host. The
  script uses Landlock `workspace` instead and removes Grok's edit tools in
  read mode. A read task's worktree is discarded either way.
