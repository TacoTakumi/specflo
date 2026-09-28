---
round: 3
verdict: changes-requested
date: '2026-09-28'
sha: 2c12327
base: b070437
level: full
reason: ''
---

# Review round 3

## Scope reviewed

The delta b070437..2c12327 (1 commit): the full diff of checkpoint.py, service/local.py, status.py and tests/test_hosted_parity.py, with every caller of build_status/build_checkpoint/write_checkpoint/review_prompt (cli.py status, checkpoint, task done, advance, review prompt; hook.py reseed and session start; daemon routes, seat and workitems); ran test_hosted_parity.py alone (9 passed) and against b070437's source (the new daemon-root test fails there), uv run pytest once (3918 passed, 1 skipped, 137.4 s), and a live_daemon probe of doc show checkpoint, status --json, checkpoint --json and the local fallback.

## Earlier findings

- F-07 open

## Findings

- F-08 (should-fix) src/specflo/service/local.py:491-495 write_checkpoint on a hosted service still calls checkpoint.build_checkpoint with test_command None, so checkpoint.py:114-115 falls back to the daemon root's cfg.test_command: every hosted mutation (task done, checkpoint, advance) rewrites the daemon's checkpoint.md naming the daemon's command, which a client reads with 'specflo doc show <slug>/checkpoint' (the file task done reports as 'Checkpoint saved'), while a local checkout with none set names none (and with one set, the hosted file names the daemon's instead of the checkout's); reproduced with the live_daemon harness. The new docstrings (checkpoint.py:65-67, status.py:28-31, local.py _suite_command) claim a daemon never names its own. Route write_checkpoint through the same rule (pass the caller's test_command, or '' on a hosted service) and add 'doc show checkpoint' to the daemon-root parity test

## Verdict
