---
title: Python 3.9 CI rows must stay on ubuntu-24.04 (no 3.9 build for Ubuntu 26.04)
author: Federico
date: 2026-10-25
branch: ci/pin-py39-ubuntu-2404
cost: { minutes: 3, tokens: 47206 }
id: l-c39bec0d
scope: repo
status: active
src: agent
touches:
  - ".github/workflows/integration.yml"
  - ".github/workflows/validate.yml"
---

## Decided
`validate` (validate.yml) and `test-pip-install` (integration.yml) pick `ubuntu-24.04` only for the 3.9 matrix row; every other version uses `ubuntu-latest`. The user's Ubuntu testing has all been on 24.04.

## Tried and ruled out
- Leaving 3.9 on `ubuntu-latest`: after `ubuntu-latest` became Ubuntu 26.04 (Oct 2026), setup-python fails with "version '3.9' ... not found for Ubuntu 26.04". actions/python-versions builds 3.9.x only for 22.04/24.04, because 3.9 is EOL. That blocked the required `validate (3.9)` check (PR #140, fixed in #141).
- Dropping 3.9: it's a breaking change, so it belongs in a minor release (6.3.0), not a CI fix.

## Watch out
Don't "simplify" the conditional `runs-on` back to `ubuntu-latest`. When ubuntu-24.04 runners are retired, drop 3.9 support or switch to `uv python install 3.9`.
