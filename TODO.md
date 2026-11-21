# Firstchair to-do list

[Roadmap and release gates](ROADMAP.md)

## Foundation · 0.0.0

- [x] Create and verify the independent public repository, initial branch and upstream tracking.
- [x] Verify an installable package, version and help output, isolated tests and configuration defaults.
- [x] Record upstream revision, license and reuse decision. Inventory repository-local instructions, skills and available integrations.
- [x] Run starter CI on GitHub and confirm release automation can build and verify distribution artifacts.
- [x] Require annotated public tags named `v` plus the package version, with changelog notes, before a GitHub Release.
- [ ] Confirm default-branch protection and release permissions, then mark the baseline release.

- [x] Demonstrate an offline plan, review and synthesis handoff with persisted status.

## MVP · 0.1.0a1

- [x] Document supported Codex, Claude Code and Grok Build command and event contracts using versioned sources and sanitized fixtures.
- [x] Build one offline vertical slice with a synthetic CLI, config validation, explicit provider selection, bounded execution and durable state.
- [x] Test malformed responses, output limits, stage timeouts, saved-state reads and classified failures.
- [x] Clean up POSIX workers and their process group on stage failure or timeout.
- [x] Verify Ctrl-C during synthetic worker startup reaps the process and records an interrupted run.
- [x] Recover abandoned synthetic run records after a controller crash on POSIX.
- [x] List saved run IDs, stages, and statuses without printing task or result content.
- [x] Verify bundled POSIX synthetic worker exit after controller SIGKILL.
- [x] Run provider subprocesses in a temporary workspace and reap a POSIX grandchild on timeout.
- [x] Keep provider children on an environment allowlist and redact secret values in saved run records.
- [x] Kill live provider process groups on timeout and interruption, and run them read-only in a temporary or chosen workspace.
- [ ] Verify a successful Claude Code 2.1.329 headless reply on an account with headless allowance.
- [x] Preview selected stages and execution limits without starting workers.
- [x] Add read-only live adapters for Codex, Claude Code and Grok Build with version gates, fake-CLI tests and a recorded host run.
- [x] Exercise offline Codex and Claude Code output parsing with synthetic fixtures.
- [x] Validate Grok Build 1.0.77 streaming-json framing offline. Reply and usage acceptance stay disabled.
- [x] Verify a structured synthetic Leader-to-worker review and synthesis with explicit run selection.
- [x] Enforce a shared time budget across synthetic stages and test budget rejection.
- [x] Enforce per-stage request byte budgets and expose synthetic limits through explicit TOML configuration.
- [x] Report offline usage as unknown and explain provider readiness and routing.
- [x] Let Claude Code, Codex and Grok Build sessions lead the maintainer dispatcher and verify automatic worker selection with synthetic CLIs.
- [x] Record version-validated token usage for Codex delegation summaries and mark unavailable telemetry as unknown.
- [x] Defer optional Claude Swap support for alpha and document acceptance cases for a future bridge.
- [x] Verify installed-package examples locally and add a release-wheel smoke check.
- [x] Install the worktree dispatcher as `firstchair delegate` and verify live Codex and Grok read jobs on the development host.
- [x] Verify the hosted support matrix and publish the alpha prerelease.

## Conductor desk

The primary seat, whether Claude Code, Codex, or Grok Build, is the conductor.
It assigns parts, starts its own provider and the other two, and integrates.
Workers do not commit. A Grok worker still cannot start its own subagents.
The conductor starts another Grok process when the seat is `self`.

- [x] Resolve a score before any process starts. `self` is the lead's provider.
      `auto` stays on the other providers and can reach Claude Code when the
      preferred worker is absent. A named seat must be on PATH.
- [x] Run that score one part at a time in isolated worktrees. Record a board
      without task text or replies. Leave later parts unstarted when one fails.
- [x] Dispatch Claude Code as a restricted worker. Read parts get Read, Grep,
      and Glob. Write parts also get Edit and Write. Command tools stay off.
      A zero exit with a reply the versioned parser rejects is a failed part.
- [x] Run independent parts together, at most four at a time.
- [x] Let the conductor cancel the running part. `conduct cancel NAME` stops
      the running parts of a live score and leaves later parts unstarted.
- [x] Let the conductor send a follow-up part that can read the previous reply
      without writing that reply into the saved board. A part names the
      earlier part with `after`.
- [x] Resume a score whose conductor process died, without replaying a part
      that already completed. A write part that left a worktree or branch is
      refused until the lead has reviewed and finished it.
- [x] Let a resumed follow-up part read an earlier reply. The reply is not
      saved, so resume runs the earlier read part again. An earlier write
      part is not replayed, and resume refuses that score.
- [x] Refuse a Grok write worker unless the run passes `--allow-grok-write`,
      and keep Grok out of automatic write selection.
- [x] Record a lead session ID on every score and delegate job, and refuse
      cancel, resume, collect, and finish from another session.
- [ ] Give the Grok read worker the deny rules the live adapter uses, then
      verify the command against the installed Grok CLI.
- [ ] Run a real score with a limit above 1, a cancel, and a resume against
      the installed provider CLIs. Coverage so far is synthetic.

## Beta and release

- [ ] Implement guarded recovery, eligible-provider fallback, bounded concurrency and explicit workspace isolation.
- [ ] Validate a Claude worker permissions contract and verified usage telemetry for Claude and Grok.
- [x] Add native sign-in and private named provider homes, with sanitized login and Claude inference checks.
- [ ] Verify simultaneous signed-in accounts on each supported host and validate opt-in account quota snapshots and per-process profile selection, with freshness, cooldowns and explicit account policy.
- [x] Rotate among signed-in accounts before each stage using real usage read from Codex and Claude Code, cooldowns and optional budgets, and record the selection rationale.
- [ ] Read Grok Build usage when its CLI offers a headless way. Read Claude usage from the stage's own output so no extra prompt is needed.
- [x] Rotate accounts for `delegate` and `conduct` workers.
- [ ] Verify rotation across several real signed-in accounts of one provider.
- [ ] Route among models using task requirements.
- [ ] Verify state backup, restart, migration and workspace recovery paths with synthetic data.
- [ ] Rehearse install and upgrade on advertised platforms. Record opt-in provider smoke results.
- [ ] Cut a beta, triage compatibility feedback, and close release-blocking reliability and security defects.
- [ ] Freeze the supported interface, build the release candidate, and validate support, release and rollback instructions.
- [ ] Publish the stable distribution and confirm installation from release assets.

## After the first stable release

- [ ] Create issue epics for task DAGs and review, resource estimation, reporting and packaging.
- [ ] Write a design and contract tests before introducing provider extensions or an MCP control interface.
- [ ] Publish public interface and deprecation commitments for 1.0.
