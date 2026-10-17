---
title: "Trail setup: what is committed and what stays local"
author: Federico
date: 2026-10-17
branch: chore/trail-setup
cost: { minutes: 1, tokens: 40719 }
touches:
  - TODO
  - AGENTS.md
  - opencode.json
---

## Decided
Commit .trail/ (hook.cjs, README, learnings), the AGENTS.md Trail section and opencode.json. .trail/cache/ stays local through .trail/.gitignore.
.claude/settings.json stays local: .gitignore ignores .claude/*, and the file holds machine-specific permissions. Do not force-add it.

## Tried and ruled out
Trail's README says `git add .claude .mcp.json .trail .gitignore .ignore`. This repo has no .mcp.json, and .gitignore/.ignore did not change, so that command does not apply as written.

## Watch out
AGENTS.md says "Don't add startup context-injecting hooks", and Trail's SessionStart hook does exactly that. Ask the maintainer before sharing the hook in a committed settings file.
