---
title: Unit tests fail inside a running container because container detection is not mocked
author: Federico
date: 2026-11-13
branch: main
cost: { minutes: 2, tokens: 18404 }
id: l-15fe16d5
scope: repo
status: active
src: agent
touches:
  - tests/conftest.py
  - src/eneru/utils.py
---

## Watch out
About 23 unit tests pass on CI, on bare metal and during `docker build`, but fail when pytest runs inside a running Docker container (OSS Scanner image, devcontainers). Container detection looks for /.dockerenv, which exists in `docker run` but not during a BuildKit build. Tests that expect the bare-metal behaviour then get container defaults: an extra SSH known_hosts option, a different wall/logger path, and a config TUI save that asks for confirmation.

## Decided
Not fixed yet. Planned fix: an autouse fixture in tests/conftest.py that pins container detection to "not in a container". Tests that simulate a container keep patching it themselves.

## Tried and ruled out
Leaked state from the build-time test run: /var/lib/eneru did not exist in the image, so that was not the cause.

In the code:
- running_in_container() in src/eneru/utils.py:229 checks /.dockerenv, /run/.containerenv, KUBERNETES_SERVICE_HOST, $container and /proc/1/cgroup.
- runtime_default_ssh_options() adds UserKnownHostsFile=/var/lib/eneru/ssh/known_hosts in containers.
- Reproduce: build the image, then `docker run --network none <img> bash -c 'cd /src && pytest -m unit -q'`.
