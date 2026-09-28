---
round: 1
verdict: changes-requested
date: '2026-09-28'
sha: 94f79fc
base: ''
level: full
reason: ''
---

# Review round 1

## Scope reviewed

The whole branch 5a0c3d1..94f79fc (24 commits): the full diff of src/specflo (workflow, status, checkpoint, guide, cli, review, service protocol/local, with the daemon wire path), tests/waits.py, tests/leakcheck.py, the pool rig cleanup, every migrated test module and each old wait_until call site, the new structural, timeout, leak, wait and skill tests, both skills, pyproject/uv.lock, README and CHANGELOG, against spec.md and plan.md; ran uv run pytest once (3916 passed, 1 skipped, 130.6 s, nothing left behind), -n 0 and COLUMNS=40 checks, the formerly flaky tests alone 3 times, and a hosted-versus-local probe of the status and checkpoint hints.

## Findings

- F-01 (should-fix) src/specflo/status.py:61 and src/specflo/checkpoint.py:112 read test_command from the cfg of whoever builds the payload, which for a hosted project is the daemon (LocalProjectService.build_status/build_checkpoint on the daemon root): with test_command set in the checkout, a hosted 'specflo status' and 'specflo checkpoint' (and the reseed prompt) print the whole-suite hint without it, while 'advance' and 'review prompt' name it (reproduced with the test_hosted_parity harness); pass the checkout's test_command to these hosted calls as review_prompt does, and add a hosted parity test for the hints
- F-02 (should-fix) tests/pool/test_poolstore.py:484-504 still names the console slot 'ops-console', a personal name, so the tests still hold it against REQ-20's 'the tests hold no operator first name'; the acceptance regex misses the 'opss-' form. Rename it to a neutral slot name
- F-03 (nit) skills/specflo-execute/SKILL.md:79-80 still says 'Review rounds run only the tests for the files in scope', which is false once test_command is set (the brief then tells the reviewer to run it each round); say the rounds run what the brief says
- F-04 (nit) tests/pool/test_sandbox_scope.py:139 'path =Path(...)' is missing the space after '=' (added by this branch)
- F-05 (nit) README.md (Development) and CHANGELOG tell a slow machine to set SPECFLO_TEST_WAIT_SCALE=3, but the 120 s pytest-timeout limit does not scale, so a test whose scaled waits add up past 120 s is cut off; say to raise --timeout too, or scale the limit in conftest
- F-06 (nit) tests/leakcheck.py:105-121 (_scan) kills any process of this user whose command line names the test's tmp_path, not only processes the test started: an operator's 'tail -f <tmp_path>/log' or an agent shell whose command names the path is SIGKILLed and reported as the test's leak (seen when probing reap by hand). The docstring should say so, or skip processes that started before the test

## Verdict
