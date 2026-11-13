---
title: Unit tests pin the runtime to bare metal so they pass inside containers
author: Federico
date: 2026-11-13
branch: fix/tests-container-runtime
cost: { minutes: 5, tokens: 30010 }
id: l-6403dcf1
scope: repo
status: active
replaces: l-15fe16d5
src: agent
touches:
  - tests/conftest.py
  - tests/test_run_command.py
  - pytest.ini
---

## Watch out
Container detection reads the real machine: /.dockerenv exists under `docker run` but not during a BuildKit `docker build`. Before the fix, about 23 unit tests passed on CI and during image builds but failed inside a running container (OSS Scanner image, devcontainers).

## Decided
An autouse fixture pins all three detectors to "not in a container" for every test. A test that simulates a container patches the detector itself (later patches win). Tests of the detectors call the real functions through direct imports, which the fixture does not reach.

## Tried and ruled out
- Leaked state from the build-time test run: /var/lib/eneru did not exist in the image.
- A no-op cache_clear on the stub: the existing cache-reset fixture then cleared nothing, and the detector tests failed on a cached "container (Docker)". The stub forwards cache_clear to the real function.

In the code:
- tests/conftest.py: _pin_bare_metal_runtime patches eneru.runtime._detect_runtime_context, eneru.utils.running_in_container and eneru.deferred_delivery._running_in_container.
- tests/test_run_command.py keeps _real_running_in_container for the detector tests.
- Check inside a container without bind mounts (SELinux): `tar cf - src tests pytest.ini | docker run --rm -i --network none <img> bash -c 'tar xf - -C /src && cd /src && pytest -m unit -q'`.
