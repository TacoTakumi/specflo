---
round: 2
verdict: changes-requested
date: '2026-09-28'
sha: b070437
base: 94f79fc
level: full
reason: ''
---

# Review round 2

## Scope reviewed

The delta 94f79fc..b070437 (2 commits): the full diff of status.py, checkpoint.py, cli.py, hook.py, service/protocol.py and service/local.py with the wire, remote client and daemon route path, the new hosted parity test (run on HEAD and shown to fail on 94f79fc's source) and the poolstore rename; grepped tests/ for the operator name forms; ran the parity, status, checkpoint, hook, continuation, service, remote, daemon-route, review-prompt and poolstore tests, uv run pytest once (3917 passed, 1 skipped, 127.5 s), and a live-daemon probe with test_command set only in the daemon root.

## Earlier findings

- F-01 closed
- F-02 closed

## Findings

- F-07 (should-fix) src/specflo/status.py:65 and src/specflo/checkpoint.py:114-115 fall back to cfg.test_command when test_command is None, and a hosted client sends None whenever its checkout has no test_command (cli.py:313/326, hook.py:163/206), so the daemon reads its own root config, the case the new docstrings say is never right on a daemon: with test_command set in the daemon root's config and unset in the checkout, hosted status, checkpoint, task done and hook reseed name the daemon's command while review prompt names none (reproduced with the live_daemon harness); do not fall back on a hosted service (or send '' for unset) and add that case to the parity test

## Verdict
