---
title: "Wait for green CI before merge, release or bot reviews"
author: Federico
date: 2026-10-17
standing: true
---

## Decided
Push, wait for CI to go green, then trigger the CodeRabbit and cubic reviews. Do not merge PRs yourself unless told. The user often wants a PR held open until their own real-world test (for example a power-cut drill).

## Watch out
Do not make further commits while a bot review is pending. Wait for it, then bring the findings to the user first.

_Told to Claude in 2 past sessions here, most recently on Oct 8._
