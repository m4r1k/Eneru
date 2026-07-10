---
name: release-review
description: Mandatory pre-release deep review for minor/major releases (X.Y.0 / X.0.0). Runs a multi-agent adversarial audit of the ENTIRE repository, produces a stable-ID findings report with user triage, drives the remediation PR, then runs a post-implementation verification round with fresh lenses. Point releases (X.Y.Z, bug fixes only) are exempt. Invoke when preparing a new minor/major release, or whenever the user asks for a full-repository audit/review.
---

# Release review — full-repository adversarial audit

Automated tests prove the code does what a test author *thought to ask*. They
do not prove that nobody overlooked a way the daemon can drop a healthy host
or miss a real outage. Before every minor/major release, the whole repository
at HEAD — not just the release diff — goes through this structured audit.

ELI5: every deep clean of a big house finds dirt; that never means the house
is filthy. The point is not "zero findings" (unreachable) — it's that new
findings stop being scary. Track the severity trend, not the count.

History (why this is mandatory): the v6.0.0 pass found ~100 issues, the
v6.1.6 pass 64, the v6.1.7 pass 65 + 28 more in verification — each round's
Criticals shrank in number and blast radius and migrated from the shutdown
core toward the periphery. See "Pre-release code review" in `docs/testing.md`.

## When

- **MANDATORY before tagging any `X.Y.0` or `X.0.0` release.** Do not tag
  without a completed cycle (both rounds) for the release.
- **NOT required for patch releases** (`X.Y.Z`) — those are bug-fix only and
  inherit the minor's review. (If a "patch" has grown feature-sized, that's a
  sign it should have been a minor — review it.)
- Also usable on demand when the user asks for a full-repo audit.

## Round 1 — fan-out review (before any fixes)

1. **Spawn three specialist subagents IN PARALLEL** (one message, three
   Agent calls — sequential spawning defeats the purpose):
   - `code-reviewer` — five-axis review (correctness, readability,
     architecture, security, performance) of the whole tree. Priority:
     production-readiness of the shutdown orchestration, state machine,
     signal handling, subprocess/SSH execution, config validation, error
     paths. Remind it of the uv-venv rule from `AGENTS.md`.
   - `security-auditor` — threat model + vulnerability pass: OWASP for the
     dashboard/API, command/argument injection, secrets in logs, YAML
     safety, dependency CVEs, systemd/packaging hardening. Ask for an
     explicit list of surfaces examined and found clean.
   - `test-engineer` — coverage AND quality: run the unit suite in a uv venv
     for ground truth; find behavioral gaps (simulated-but-never-exercised
     paths, races), not just uncovered lines.
2. **Orchestrator direct checks** (main session, while agents run): docs
   freshness, CI/workflow health, dashboard accessibility signals.
3. **Merge, dedupe, and write the report** at the repo root
   (`SHIP_REVIEW.md` pattern — a working artifact, do NOT commit it):
   - Ship decision (GO/NO-GO) + a rollback plan (trigger conditions,
     per-install-path procedure, RTO).
   - **Master findings table** — `ID | Sev | Axis (source) | file:line |
     Finding | Suggested remediation | Effort`. IDs are stable `F-NNN`,
     continuing the existing series across rounds and releases, never reused.
   - Implementation guidance: findings sharing a root cause become one **fix
     group** landed as one unit (the v6.1.7 config schema gate closed seven
     findings at once); a suggested landing order; per-fix verification.
   - The report must be self-contained for a fresh agent session: no
     references to the review conversation.
4. **User triage (required before any fix).** Present an ELI5 table — one
   plain-language row per finding, concrete metaphor first, jargon second —
   so the user can drop or downgrade findings. Record drops in the report
   with the user's reason (strikethrough row + scope-rule entry). A dropped
   finding is never implemented; reviewers never return empty, so triage is
   part of the process, not an insult to it.

## Remediation PR

- Track every in-scope finding with `/goals` until closed — do not declare
  done while any non-dropped finding is open (v6.1.7 shipped its PR with one
  of 62 silently unaddressed; the tracker exists to make that impossible).
- One commit per category (fix group / silent-failures / security / perf /
  CI / tests / docs-tail), all on a single branch, delivered as **one PR**.
- Then the standard `AGENTS.md` flow: CI green → both upstream AI reviews →
  address findings (max two rounds) → **wait for the user's explicit
  greenlight**. Never self-merge.

## Round 2 — verification (after implementation, before merge/tag)

Spawn IN PARALLEL:

1. **Compliance audit** — every in-scope finding vs the actual diff:
   fixed / fixed-differently (judge the deviation) / partial / missing.
   Verify substance by reading the changed code, not commit messages.
   Explicitly re-verify every user-mandated constraint from triage.
2. **Regression review of the diff itself** — bugs the fixes introduced.
   Crucially: verify each fix works in the exact scenario its finding
   cites (three v6.1.7 fixes failed their own cited scenario).
3. **Fresh bug hunts with lenses earlier rounds did NOT use** — rotate
   among: cross-module interactions, real-world data edges (malformed/
   extreme NUT variables, odd SSH targets), time & ordering (DST,
   monotonic-vs-wall, rollover), resource lifecycle (leaks, unbounded
   growth over weeks of uptime), newest-code-meets-oldest-code.

Rules for round-2 agents: read the round-1 report first — re-reporting a
known or user-dropped finding is a task failure; findings need a concrete,
traceable failure path (no hardening wishlists); an honest "found little"
beats padding.

Output: a **separate** round-2 report (`SHIP_REVIEW_ROUND2.md` pattern —
never fold into the round-1 report), continuing the F-ID series, with a
merge/tag gate section, already-fixed markers for anything closed in the
meantime, and a "verified sound — do not re-litigate" list so later passes
don't churn. New findings go through the same user triage, then loop back
into the remediation PR; re-verify only what changed.

## Principles (learned over v6.0.0 → v6.1.7)

- **Close bug classes, not instances.** A declarative gate or a CI tripwire
  beats N spot fixes; ask "what invariant was missing?" for every cluster.
- **Finding count is not a health metric; the severity trend is.** Expect a
  long Minor/Low tail forever; reserve alarm for Criticals recurring in
  previously-audited core code.
- **A fix is not done until verified against its own cited scenario.**
- **New code carries new bugs at roughly constant density** — a review after
  a feature wave harvests that wave; that's the process working.
- Reports carry stable IDs and `file:line` anchors so any harness (Claude,
  Codex, a human) can execute them without the originating conversation.
