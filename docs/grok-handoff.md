# Firstchair handoff for Grok

> Historical checkpoint. See [provider contracts](provider-contracts.md) and
> [orchestration](orchestration.md) for current behavior and verification.

Continue the MVP toward `0.1.0a1` from the Git checkpoint that introduced
this file. The package version at this checkpoint is `0.0.0`. Read
`AGENTS.md`, `ROADMAP.md`, `TODO.md`, and
`docs/provider-contracts.md` before editing.

## What is in the checkpoint

- The offline synthetic workflow passes a structured worker review into
  synthesis, then persists the review, result, and usage provenance.
- The CLI previews routing and limits, loads optional TOML configuration,
  and enforces per-stage input and output bounds plus a shared time budget.
- POSIX recovery uses a per-run lock. The bundled synthetic worker exits
  when its controller dies. Use `firstchair runs` to find an abandoned run and
  `firstchair recover <run-id>` to mark it interrupted.
- Codex and Claude result parsers have invented offline fixtures. Live
  routing remains disabled. The release workflow checks a synthetic
  handoff and its persisted status from the installed wheel.

## Verification evidence

All 43 unit tests passed locally on Linux with Python 3.12 and 3.14. The
release smoke ran against a freshly built wheel under Python 3.12 and
validated the wheel and source distribution checksums. Hosted CI and
publication are pending.

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m firstchair doctor
git diff --check
```

## First work order

Framing validation for Grok Build 1.0.77 `streaming-json` is in tree.
`validate_grok_jsonl` checks type-tagged newline-delimited JSON only. The
next evidence still needed is a sanitized host capture of a terminal reply,
usage, and unattended tool denial. Keep the adapter disabled while those
gates are open. Any live smoke requires explicit maintainer opt-in and a
credential-isolated environment.

Grok project files live under `.grok/`: the `firstchair` agent, the
`contract-reviewer` persona, and the `firstchair` plugin with the
`provider-fixture` skill and `contract-reviewer` agent. `.grok/config.toml`
enables that plugin and asks before `scripts/live-smoke.py`. Folder trust
is required before Grok loads those project files.

Provider subprocesses now use a controller temporary workspace, and a
synthetic dry-run reports `"workspace": "temporary"`. POSIX timeout still
kills the process group, including a grandchild. Live provider process
trees are still unverified.

Provider children receive an explicit environment allowlist. Saved run
records replace secret environment values of at least 12 characters with
`[redacted]`. Live adapters stay disabled.

Public GitHub Releases use annotated tags of the form `v` plus the package
version. `python -m firstchair.release_tag` checks the tag, version, and
changelog before a tag is pushed. No tag has been created.

The other open gates are live workspace isolation and process-tree
cleanup, hosted CI across the configured matrix, and the alpha release
rehearsal. `main` tracks the public repository
https://github.com/GrayManSam/firstchair. The same history is on the private
remote `private` at https://github.com/GrayManSam/firstchair-private. Hosted
CI for that push has started and is not yet recorded as green. Keep
transcripts, local state, account information, and credentials out of
work products and Git.

## Running as a delegated worker

A Claude Code or Codex lead can dispatch Grok through
`scripts/delegate.py`. Read tasks run in a disposable worktree. Write
tasks run on branch `delegate/NAME`. Grok runs with `--sandbox workspace`
because the read-only profile needs bubblewrap, which fails on this host.
The folder must be trusted once with `grok --trust`.
