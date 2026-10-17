---
title: "Config changes must cover TUI, reference config and docs"
author: Federico
date: 2026-10-17
standing: true
---

## Decided
When adding a config option, also add it to the `eneru config` TUI (advanced section if it is an advanced option), the reference config and the documentation. Add tests, and an E2E step for shutdown-path behavior.
Changes must work with no config file at all and with the minimal example config. Do not show deprecated legacy options in the TUI.

_Told to Claude in 2 past sessions here, most recently on Oct 8._
