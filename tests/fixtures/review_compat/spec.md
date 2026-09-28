---
project: suite-cost
phase: spec
status: complete
created: 2026-09-28
updated: 2026-09-28
---

> Complete (2026-09-28). Historical record - see docs/projects/specflo-index.md. Decisions and requirements here do not bind new work unless restated.

# Spec: suite-cost

## Objective

Make the whole test suite cheap and trustworthy for the agents that run it in every project: it runs in parallel in about 2 min instead of 11, each whole-suite step and review round names one command (test_command), no test leaks a process or a locked directory, the known flaky tests pass under load, and the operator's first name leaves the tests.

## Requirements
<!-- append-only; managed by `specflo requirement add`. Stable IDs REQ-NN. -->

### REQ-01 — pytest-xdist is a dev dependency, and a plain uv run pytest runs the whole suite on a fixed number of parallel workers set in the pytest config
- Acceptance: The dev dependency group lists pytest-xdist, and the header of a plain uv run pytest reports the configured worker count (8 unless the measurements at spec time pick another)
- Derives from: D-02
- Status: superseded by REQ-22

### REQ-02 — -n 0 on the command line turns the parallel run off, so -s and pdb still work
- Acceptance: uv run pytest -n 0 tests/test_workflow.py reports no workers in its header and passes
- Derives from: D-02
- Status: active

### REQ-03 — The whole suite passes in parallel and takes at most 180 s wall time on the rig (20 logical cores)
- Acceptance: Three runs of uv run pytest in a row each exit 0 with 0 failed and 0 errors, and each takes at most 180 s by time
- Derives from: D-02
- Status: active

### REQ-04 — No test depends on the length of its tmp path or on the terminal width
- Acceptance: test_directory_invalid_missing_dir_is_rejected_before_the_subcommand passes under the parallel run, where the worker directory makes the tmp path longer
- Derives from: D-02
- Status: active

### REQ-05 — When test_command is set, every next-step hint that calls for the whole test suite names it; when it is not set, the hint text is as it is today
- Acceptance: A test sets test_command to a sentinel command and checks that the hints for no round yet and for a passing round after changes both contain it in backticks; with test_command unset both hints equal today's text
- Derives from: D-03
- Status: active

### REQ-06 — When test_command is set, the reviewer brief tells the reviewer to run it each round; when it is not set, the brief keeps today's sentence
- Acceptance: A test checks that specflo review prompt with test_command set prints an instruction to run that command under Tests and not the sentence 'Run only the tests for the files in scope'; with it unset the brief prints that sentence
- Derives from: D-03
- Status: active

### REQ-07 — The quick and execute skills tell the agent to run test_command as the whole suite when it is set, and say how to read it (specflo config get test_command)
- Acceptance: A test reads both SKILL.md files and finds specflo config get test_command at each step that runs the whole suite
- Derives from: D-03
- Status: active

### REQ-08 — This repo's specflo config sets test_command to uv run pytest
- Acceptance: specflo config get test_command in this repo prints uv run pytest
- Derives from: D-03
- Status: active

### REQ-09 — A test that leaves a process whose command line carries the test's tmp_path fails: at teardown the check waits up to 5 s (times the wait scale) for such processes to exit, then kills them and fails the test naming each pid and command
- Acceptance: A pytester test plants a test that starts a sleep with its tmp_path in the arguments and returns; that test fails with a message naming the sleep's pid and command, and the sleep process is gone afterwards
- Derives from: D-04
- Status: active

### REQ-10 — The leak check does not fail a test whose processes exit within the grace time
- Acceptance: A pytester test plants a test that starts a short sleep (1 s) with its tmp_path in the arguments and returns; that test passes
- Derives from: D-04
- Status: active

### REQ-11 — The pool test rig ends every member it started, also for a lease the test never ended: it kills each host's process group and stops each member scope the test started
- Acceptance: test_a_lease_passing_its_idle_limit_grants_the_waiting_request_with_no_other_client passes with the leak check on, and afterwards no process and no active specflo-member scope carries its tmp path
- Derives from: D-04
- Status: active

### REQ-12 — A whole-suite run leaves no test process and no member scope behind
- Acceptance: After uv run pytest exits, no process has a /tmp/pytest-of-<user> path in its command line and systemctl --user list-units 'specflo-member-*' lists no unit
- Derives from: D-04
- Status: active

### REQ-13 — No test leaves a directory pytest cannot remove: a test that takes rights away from a directory gives them back in its own teardown, whether it passed or failed
- Acceptance: A run of the piconfig remove test with a planted failure after its chmod leaves its directories with owner rwx, and after a whole-suite run the pytest temp root has no garbage-* directory
- Derives from: D-04
- Status: active

### REQ-14 — One shared wait helper serves every test; no test module defines its own wait_until
- Acceptance: A structural test finds def wait_until only in the shared helper module under tests/
- Derives from: D-05
- Status: active

### REQ-15 — The wait helper returns as soon as its condition holds, and its limits (30 s by default) scale by the SPECFLO_TEST_WAIT_SCALE setting (default 1)
- Acceptance: Unit tests of the helper: a condition that holds at once returns in under 0.1 s; with SPECFLO_TEST_WAIT_SCALE=2 a condition that never holds fails after twice the given limit (a 0.2 s limit fails between 0.4 and 1 s)
- Derives from: D-05
- Status: active

### REQ-16 — No fixed sleep in the pool and agent tests waits for something to happen; a pause that checks that something does not happen goes through the helper, scaled by the same setting
- Acceptance: A structural test finds no time.sleep( in the test modules under tests/pool and tests/agent; the stub programs and the helper module are exempt
- Derives from: D-05
- Status: superseded by REQ-21

### REQ-17 — The tests skipped as flaky or slow run again: test_accounts_402 'the first agent', the two skipped tests in test_ledger_capacity and the skipped test in test_scope_sweep
- Acceptance: The -rs report of a whole-suite run lists no skip for these tests; the only skip left is the off-host provider check in test_hosted_sandbox
- Derives from: D-05
- Status: active

### REQ-18 — The known flaky tests pass under load: test_accounts_402 'the first agent', the ledger capacity tests of FU-86 and FU-93, the web project serving test (FU-89), the console lease holders-stop test (FU-102) and the scope sweep test
- Acceptance: Each named test passes in each of the three whole-suite parallel runs of REQ-03, and passes 10 runs in a row alone
- Derives from: D-05
- Status: active

### REQ-19 — pytest-timeout limits each test to 120 s with the signal method, so a test that hangs fails, its teardown runs and the run goes on
- Acceptance: The pytest config sets timeout to the string 120 and timeout_method to signal; a pytester test plants a hanging test under a 2 s timeout and checks that it fails with a timeout, its fixture finalizer ran and the next test ran
- Derives from: D-05
- Status: active

### REQ-20 — The tests hold no operator first name: the agent names, git identities and quoted names that used it take neutral names
- Acceptance: git grep -n -i -E '\bops\b|ops-|ops@' -- tests/ prints nothing, and the tests that used those names pass
- Derives from: D-01
- Status: active

### REQ-21 — No fixed sleep in the pool and agent tests waits for something to happen; a pause that checks that something does not happen goes through the helper, scaled by the same setting
- Acceptance: A structural test parses the test modules under tests/pool and tests/agent and finds no call to time.sleep in their code; text in strings (scripts written for subprocesses), the stub programs and the helper module are exempt
- Derives from: D-05
- Supersedes: REQ-16
- Status: active

### REQ-22 — pytest-xdist is a dev dependency, and a plain uv run pytest runs the whole suite on 8 parallel workers set in the pytest config
- Acceptance: The dev dependency group lists pytest-xdist, and the header of a plain uv run pytest reports 8 workers
- Derives from: D-02
- Supersedes: REQ-01
- Status: active

## Boundaries
### In scope

- The pytest config and dev dependencies: pytest-xdist and pytest-timeout.
- The test infrastructure: the shared wait helper, the leak check, the pool rig's cleanup and the chmod test's teardown.
- The flaky and skipped tests named in REQ-17 and REQ-18, and the path-length check of REQ-04.
- The next-step hints, the reviewer brief, and the quick and execute skills, for test_command.
- test_command in this repo's local specflo config (the .specflo directory is not in git, so this is a setting on the rig).
- The name changes in the tests.
- The follow-ups this project closes: FU-61, FU-85, FU-86, FU-87, FU-88, FU-89, FU-93, FU-94, FU-102.

### Out of scope

- Removing the operator's first name from older commits, and a first-name check in release-kit.
- A marker that leaves pool or agent tests out of the default run.
- Test-impact selection tools (pytest-testmon, pytest-impacted).
- Leaks in production code: daemon shutdown does not end leases, and the bwrap --die-with-parent race.
- Setting test_command in other repos, and the plan skill's fast-level text.

## Open questions

None. The worker count is 8: measured on the rig (14 cores, 20 threads) on 2026-09-28, 4 workers took 167 s, 8 took 108 s and 12 took 80 s, each with only the path-length failure of REQ-04. 12 saves 28 s but adds load, which is what makes the flaky tests fail, and the rig runs other work beside the tests.

## Canonical refs

- docs/projects/suite-cost/brainstorm.md (D-01 to D-05, Research)
- Agent Wiki: research/pytest-suites-with-real-subprocesses-speed-test-selection-leaks-and-load-flakes-2026-09.md
- docs/design/2026-09-25-review-round-convergence.md ("Related friction")
- https://pytest-xdist.readthedocs.io/en/stable/distribution.html
- https://github.com/pytest-dev/pytest-timeout
