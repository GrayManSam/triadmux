# Firstchair roadmap

Firstchair coordinates local coding CLIs through a Python controller. The first useful workflow takes a user-approved task, runs a bounded provider invocation, and records an inspectable outcome. Each milestone expands that workflow through observable acceptance tests.

| Stage | Git tag | Python version | Goal |
| --- | --- | --- | --- |
| Foundation | `v0.0.0` | `0.0.0` | Independent repository and a reproducible local development loop |
| First alpha | `v0.1.0a1` | `0.1.0a1` | A complete, bounded Leader-to-worker workflow |
| First beta | `v0.1.0b1` | `0.1.0b1` | Validate recovery, portability and real provider compatibility |
| Release candidate | `v0.1.0rc1` | `0.1.0rc1` | Freeze public interfaces and rehearse release and upgrade |
| First stable release | `v0.1.0` | `0.1.0` | Publish a documented, supported first release |
| Compatibility commitment | `v1.0.0` | `1.0.0` | Commit to versioned CLI, configuration and adapter contracts |

Increment the prerelease counter for each new build in a stage. Patch releases fix defects in a stable line. Minor releases add capabilities. Document breaking changes and migration paths during 0.x, then reserve breaking public interface changes for major releases from 1.0 onward.

## Foundation · 0.0.0

- Create the public repository and local Python package with license, setup instructions, contribution policy, security contact, changelog and test commands.
- Document the upstream revision, license and reuse decision. Record evidence for each supported provider contract and the state of repository-scoped agent tooling.
- Establish pull request CI and tag-triggered packaging. Keep provider authentication and live usage outside ordinary CI.
- Exit gate. A clean checkout runs the local verification commands and installs an artifact that reports version 0.0.0. The public repository shows the same revision and a recorded CI run.

## MVP and alpha · 0.1.0a1

- Implement diagnostics and configuration first. Discover Codex, Claude Code and Grok Build and report missing, installed, and verified capabilities.
- Add a shared process runner with explicit arguments, bounded output, deadlines and cancellation. Develop and test the normalized event contract with a synthetic CLI before adding real adapters.
- Persist task attempts and terminal outcomes. Ship inspectable execution state, a dry-run path, and a user-approved, read-only Leader-to-worker handoff that returns a structured result for synthesis.
- Add explicit provider selection, readiness checks, conservative budgets and an explanation of eligible candidates. Distinguish measured, estimated and unknown usage. Evaluate an optional claude-swap bridge through its documented interfaces.
- Exit gate. An installed artifact completes the synthetic Leader-to-worker demonstration. Tests cover absent tools, malformed events, process failure, deadline and cancellation, budget rejection and secret redaction. Document real-provider smoke results and any remaining unverified capabilities.

## Beta · 0.1.0b1

- Let the primary seat conduct a score: assign parts to its own provider and to Codex, Claude Code, and Grok Build, run them one at a time in isolated worktrees, and stop when a part fails.
- Expand test coverage to real provider contract smoke checks run by maintainers with explicit opt-in. Publish a supported platform, Python and provider version matrix.
- Verify restart recovery, bounded parallel read-only tasks, worktree isolation for approved write tasks, and transactional state migrations. Preserve completed work and require review when an interrupted write has an ambiguous outcome.
- Exercise fallback against classified transient failures, with cooldowns and bounded retries for eligible idempotent work. Document the difference between observed usage and charges.
- Exit gate. Successful clean-install and upgrade rehearsals, one credential-isolated smoke for each advertised provider, and recovery tests for interrupt, timeout, and partial failure. Resolve release-blocking security and data integrity defects.

## Release candidate and stable · 0.1.0rc1 → 0.1.0

- Freeze the first supported CLI, configuration and state interfaces. Document configuration precedence, permission boundaries, troubleshooting, data retention, backup and recovery.
- Verify a wheel and source distribution from the release commit. Compare package metadata, Git tag and changelog. Rehearse fresh install and upgrade from the prior supported build before publishing.
- Exit gate. Required CI checks pass on the release commit. A maintainer approves the release notes, known limitations, artifact checksums and rollback procedure. Verify the published artifacts from a clean environment.
- Service the release with focused patches. For a faulty release, publish a corrected version and document recovery. Preserve Git tag history.

## Post-release development

| Area | Planned work | Acceptance evidence |
| --- | --- | --- |
| Collaboration | User-approved task DAGs, cross-model review, context compression, capability profiles and improved interruption recovery | Bounded fixtures preserve dependency order, completed work and context limits |
| Resource intelligence | Historical cost and latency estimates, quota-window forecasts and explainable policy selection | Backtests compare estimates with observed values and document uncertainty |
| Developer experience | Task and usage views, collaboration presets, exportable reports, provider plugin API and optional MCP control interface | Contract tests pass and operators can inspect the permissions and data exposed |
| Hardening | Wider packaging, migration tooling, performance profiling and security review | Clean installs and upgrades on each advertised platform with repeatable verification |
| 1.0 readiness | Version stable public interfaces and publish compatibility and deprecation policy | Upgrade fixtures and compatibility tests cover every supported interface |

## Development and release lifecycle

1. Track work in GitHub Issues. For each change, define scope, acceptance criteria and test evidence. Record durable architecture decisions in the repository.
2. Use short-lived branches and focused pull requests. Link the issue, describe user-visible changes and document verification, skipped checks and migration effects.
3. Run lint, tests and an installed-package smoke check on pull requests and protected-branch updates. Require passing checks and maintainer review where available, including review of workflow, state and provider changes. Use squash merges for independent work and keep `main` releasable.
4. Build on annotated `v` tags that match the package version and changelog. Verify artifacts before attaching them to a GitHub Release, and mark `v0.0.0`, alpha, beta, and candidate builds as prereleases. Published tags stay immutable. Give fork pull request CI read-only permissions and scope write permissions to the release job. Keep authentication for provider smoke checks local to the maintainer.
5. Start with standard GitHub-hosted runners for the public repository and use short-lived CI artifacts. Standard runner compute is free for public repositories. Review storage usage and keep long-lived distribution files on versioned Releases. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
6. Triage reports against the published support window. Handle security reports through the project security policy, then add regression coverage, publish fixes and update affected release notes.
