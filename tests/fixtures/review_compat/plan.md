---
project: suite-cost
phase: plan
status: complete
created: 2026-09-28
updated: 2026-09-28
---

# Plan: suite-cost

## Approach

Turn on pytest-xdist (8 workers) and pytest-timeout first, so every later task runs its tests in parallel with an outer limit. Then give the workflow one whole-suite command (test_command). Then replace the 19 copied wait_until functions and the 51 fixed sleeps with one shared helper whose limits scale by SPECFLO_TEST_WAIT_SCALE, file group by file group. With waits in place, add the leak check and make the pool rig end what it starts, fix the tests the check catches, and fix the flaky and skipped tests at their root cause. A last task proves the whole suite: three parallel runs within 180 s, nothing left behind.

## Global constraints

- Each task's Verify runs the tests that task adds or changes, with the parallel run on (addopts -n 8 once T-02 lands). The whole suite runs in the last task.
- The shared helper module sits in tests/ beside reviewhelp.py and is imported the same way (tests/ is on sys.path through tests/conftest.py).
- A task that closes a follow-up names it with specflo task done --closes FU-NN.
- Keep CHANGELOG.md and README.md pure ASCII.

## Milestones

### M-01 — Parallel and bounded
- Exit:
  - uv run pytest reports 8 workers
  - -n 0 turns the parallel run off
  - a hanging test fails at its limit and the run goes on
  - the tests hold no operator first name

### M-02 — One whole-suite command
- Exit:
  - hints name test_command when set
  - the reviewer brief tells the reviewer to run test_command
  - the quick and execute skills name specflo config get test_command

### M-03 — Shared waits
- Exit:
  - one wait_until, in the shared helper
  - no time.sleep call in the pool and agent test modules
  - SPECFLO_TEST_WAIT_SCALE scales every limit

### M-04 — No leaks
- Exit:
  - a planted leak fails its test and is killed
  - the pool rig ends every member it started
  - no locked directory is left behind

### M-05 — Flaky tests fixed and suite proven
- Exit:
  - the skipped tests run again
  - the known flaky tests pass under load
  - three whole-suite runs pass within 180 s and leave nothing behind

## Tasks
<!-- append-only; managed by `specflo task add`. Stable IDs T-NN. -->

### T-01 — Give the tests neutral names in place of the operator's first name
- Acceptance: git grep -n -i -E '\bops\b|ops-|ops@' -- tests/ prints nothing, and the four changed test files pass
- Verify: git grep -n -i -E '\bops\b|ops-|ops@' -- tests/; uv run pytest tests/pool/test_console_attach.py tests/pool/test_member_git_identity.py tests/pool/test_member_git_sandbox.py tests/test_web_pool.py
- Implements: REQ-20
- Files: tests/pool/test_console_attach.py, tests/pool/test_member_git_identity.py, tests/pool/test_member_git_sandbox.py, tests/test_web_pool.py
- Scope: Small
- Milestone: M-01
- Progress: done
- Status: active

### T-02 — Run the suite on 8 pytest-xdist workers by default
- Acceptance: The dev group lists pytest-xdist; uv run pytest tests/test_workflow.py reports 8 workers and passes; uv run pytest -n 0 tests/test_workflow.py reports no workers and passes
- Verify: uv run pytest tests/test_workflow.py; uv run pytest -n 0 tests/test_workflow.py
- Implements: REQ-22, REQ-02
- Files: pyproject.toml, uv.lock
- Scope: Small
- Milestone: M-01
- Progress: done
- Status: active

### T-03 — Make the missing-directory check independent of the tmp path length and the terminal width
- Acceptance: test_directory_invalid_missing_dir_is_rejected_before_the_subcommand passes under the parallel run, and with COLUMNS=40
- Verify: uv run pytest tests/test_cli.py -k missing_dir; COLUMNS=40 uv run pytest tests/test_cli.py -k missing_dir
- Implements: REQ-04
- Depends on: T-02
- Files: tests/test_cli.py
- Scope: Small
- Milestone: M-01
- Progress: done
- Status: active

### T-04 — Limit each test to 120 s with pytest-timeout's signal method
- Acceptance: The pytest config sets timeout to the string 120 and timeout_method to signal; a pytester test plants a hanging test under a 2 s timeout and checks it fails with a timeout, its fixture finalizer ran and the next planted test ran
- Verify: uv run pytest tests/test_suite_timeout.py
- Implements: REQ-19
- Depends on: T-02
- Files: pyproject.toml, uv.lock, tests/conftest.py, tests/test_suite_timeout.py (new)
- Scope: Medium
- Milestone: M-01
- Progress: done
- Status: active

### T-05 — Name test_command in the next-step hints that call for the whole suite
- Acceptance: With test_command set to a sentinel, the no-round hint and the passing-after-changes hint contain it in backticks, through status, checkpoint, guide and advance; with it unset both hints equal today's text
- Verify: uv run pytest tests/test_workflow.py tests/test_status.py tests/test_checkpoint.py
- Implements: REQ-05
- Files: src/specflo/workflow.py, src/specflo/status.py, src/specflo/checkpoint.py, src/specflo/guide.py, src/specflo/cli.py, tests/test_workflow.py
- Scope: Medium
- Milestone: M-02
- Progress: done
- Status: active

### T-06 — Tell the reviewer to run test_command each round when it is set
- Acceptance: specflo review prompt with test_command set prints an instruction to run that command under Tests and not 'Run only the tests for the files in scope'; with it unset the brief prints that sentence; a hosted project gives the same brief as a local one
- Verify: uv run pytest tests/test_review_prompt.py tests/test_hosted_parity.py
- Implements: REQ-06
- Files: src/specflo/review.py, src/specflo/service/protocol.py, src/specflo/service/local.py, src/specflo/cli.py, tests/test_review_prompt.py, tests/test_hosted_parity.py
- Scope: Small
- Milestone: M-02
- Progress: done
- Status: active
- Note: 2026-09-28 [Design] A daemon builds the brief from its own config, so the client passes its checkout's test_command, the way it passes sha to review start

### T-07 — Point the quick and execute skills at specflo config get test_command for the whole suite
- Acceptance: A test reads both SKILL.md files and finds specflo config get test_command at each step that runs the whole suite
- Verify: uv run pytest tests/test_skill_test_command.py
- Implements: REQ-07
- Files: skills/specflo-quick/SKILL.md, skills/specflo-execute/SKILL.md, tests/test_skill_test_command.py (new)
- Scope: Small
- Milestone: M-02
- Progress: done
- Status: active

### T-08 — Set test_command to uv run pytest in this repo's local specflo config
- Acceptance: specflo config get test_command prints uv run pytest
- Verify: specflo config get test_command
- Implements: REQ-08
- Depends on: T-02
- Scope: Small
- Milestone: M-02
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Set in .specflo/config.yaml, which is gitignored, so the task has no commit

### T-09 — Add the shared wait helper with limits scaled by SPECFLO_TEST_WAIT_SCALE
- Acceptance: Unit tests: a condition that holds at once returns in under 0.1 s; with SPECFLO_TEST_WAIT_SCALE=2 a 0.2 s limit fails between 0.4 and 1 s; the default limit is 30 s; a settle pause scales the same way
- Verify: uv run pytest tests/test_waits.py
- Implements: REQ-15
- Depends on: T-02
- Files: tests/waits.py (new), tests/test_waits.py (new)
- Scope: Small
- Milestone: M-03
- Progress: done
- Status: active

### T-10 — Move the top-level tests and the pool runner group onto the shared helper
- Acceptance: test_chat_pump, test_seat, pool test_runner, test_runner_sandbox and test_lease_request define no wait_until and call no time.sleep, and they pass
- Verify: uv run pytest tests/test_chat_pump.py tests/test_seat.py tests/pool/test_runner.py tests/pool/test_runner_sandbox.py tests/pool/test_lease_request.py
- Implements: REQ-14, REQ-21
- Depends on: T-09
- Files: tests/test_chat_pump.py, tests/test_seat.py, tests/pool/test_runner.py, tests/pool/test_runner_sandbox.py, tests/pool/test_lease_request.py, tests/pool/test_reload.py
- Scope: Medium
- Milestone: M-03
- Progress: done
- Status: active
- Note: 2026-09-28 [Design] test_reload.py joins the files: its one assert-not wait (a check that something does not happen) breaks once test_runner's wait_until raises at its limit

### T-11 — Move the pool waiting and bridge group onto the shared helper
- Acceptance: test_waiting, test_waiting_notice, test_sandbox_scope, test_bridge_filter and test_bridge_member call no time.sleep, and they pass
- Verify: uv run pytest tests/pool/test_waiting.py tests/pool/test_waiting_notice.py tests/pool/test_sandbox_scope.py tests/pool/test_bridge_filter.py tests/pool/test_bridge_member.py
- Implements: REQ-21
- Depends on: T-09
- Files: tests/pool/test_waiting.py, tests/pool/test_waiting_notice.py, tests/pool/test_sandbox_scope.py, tests/pool/test_bridge_filter.py, tests/pool/test_bridge_member.py
- Scope: Medium
- Milestone: M-03
- Progress: done
- Status: active

### T-12 — Move the pool console, grant, team and scope-sweep group onto the shared helper
- Acceptance: test_console_attach, test_service_grant, test_team_lease and test_scope_sweep call no time.sleep, and they pass
- Verify: uv run pytest tests/pool/test_console_attach.py tests/pool/test_service_grant.py tests/pool/test_team_lease.py tests/pool/test_scope_sweep.py
- Implements: REQ-21
- Depends on: T-09, T-01
- Files: tests/pool/test_console_attach.py, tests/pool/test_service_grant.py, tests/pool/test_team_lease.py, tests/pool/test_scope_sweep.py
- Scope: Small
- Milestone: M-03
- Progress: done
- Status: active

### T-13 — Move the agent CLI, contract, host and lease group onto the shared helper
- Acceptance: test_cli_lifecycle, test_cli_prompt, test_contract, test_herdr_integration, test_host, test_lease_activity, test_lease_broadcast and test_lease_wall define no wait_until and call no time.sleep, and they pass
- Verify: uv run pytest tests/agent/test_cli_lifecycle.py tests/agent/test_cli_prompt.py tests/agent/test_contract.py tests/agent/test_herdr_integration.py tests/agent/test_host.py tests/agent/test_lease_activity.py tests/agent/test_lease_broadcast.py tests/agent/test_lease_wall.py
- Implements: REQ-14, REQ-21
- Depends on: T-09
- Files: tests/agent/test_cli_lifecycle.py, tests/agent/test_cli_prompt.py, tests/agent/test_contract.py, tests/agent/test_herdr_integration.py, tests/agent/test_host.py, tests/agent/test_lease_activity.py, tests/agent/test_lease_broadcast.py, tests/agent/test_lease_wall.py
- Scope: Medium
- Milestone: M-03
- Progress: done
- Status: active

### T-14 — Move the agent list, policy, socket, stop, structure, transcript and TUI group onto the shared helper
- Acceptance: test_list, test_policy, test_socket, test_stop, test_structure, test_transcript, test_tui_e2e, test_tui_start, test_tui_stop and test_adoption_e2e define no wait_until and call no time.sleep; the non-rig ones pass and the rig ones still collect
- Verify: uv run pytest tests/agent/test_list.py tests/agent/test_policy.py tests/agent/test_socket.py tests/agent/test_stop.py tests/agent/test_structure.py tests/agent/test_transcript.py tests/agent/test_tui_start.py tests/agent/test_tui_stop.py; uv run pytest -m rig --co -q tests/agent/test_tui_e2e.py tests/agent/test_adoption_e2e.py
- Implements: REQ-14, REQ-21
- Depends on: T-09
- Files: tests/agent/test_list.py, tests/agent/test_policy.py, tests/agent/test_socket.py, tests/agent/test_stop.py, tests/agent/test_structure.py, tests/agent/test_transcript.py, tests/agent/test_tui_e2e.py, tests/agent/test_tui_start.py, tests/agent/test_tui_stop.py, tests/agent/test_adoption_e2e.py
- Scope: Medium
- Milestone: M-03
- Progress: done
- Status: active

### T-15 — Add the structural test that keeps one wait helper and no fixed sleeps
- Acceptance: The test finds def wait_until only in the shared helper module, and by AST no time.sleep call in the test modules under tests/pool and tests/agent (text in strings, the fake pi and provider programs, and the helper are exempt); it fails on a planted copy or call
- Verify: uv run pytest tests/test_suite_structure.py
- Implements: REQ-14, REQ-21
- Depends on: T-10, T-11, T-12, T-13, T-14
- Files: tests/test_suite_structure.py (new)
- Scope: Small
- Milestone: M-03
- Progress: done
- Status: active

### T-16 — Fail a test that leaves a process carrying its tmp_path
- Acceptance: A pytester test plants a test that starts a sleep with its tmp_path in the arguments and returns: it fails naming the pid and command, and the sleep is gone; a planted test whose 1 s sleep exits within the 5 s grace time passes; the grace time scales with SPECFLO_TEST_WAIT_SCALE
- Verify: uv run pytest tests/test_leakcheck.py
- Implements: REQ-09, REQ-10
- Depends on: T-09, T-04
- Files: tests/leakcheck.py (new), tests/conftest.py, tests/test_leakcheck.py (new)
- Scope: Medium
- Milestone: M-04
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] The leak is reported as an error at teardown, not a call failure: the check must run after fixture teardown, when the call report is already sent. The run still fails and names each pid and command.

### T-17 — Make the pool rig end every member it started, closing FU-87 and FU-94
- Acceptance: Rig cleanup kills each host's process group and stops each specflo-member scope whose processes carry the rig's tmp path; test_a_lease_passing_its_idle_limit_grants_the_waiting_request_with_no_other_client passes with the leak check on and leaves no process or scope carrying its tmp path
- Verify: uv run pytest tests/pool/test_waiting.py -k idle_limit; systemctl --user list-units --all --no-legend 'specflo-member-*'
- Implements: REQ-11
- Depends on: T-16, T-10, T-11
- Files: tests/pool/test_runner.py, tests/pool/conftest.py, tests/pool/test_waiting.py
- Scope: Medium
- Milestone: M-04
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Root cause: a second lease the test never ended was still starting at teardown; killing host and pi pids raced bwrap arming die-with-parent (sandbox init and socat lived on in the scope) and systemd-run making the scope (an empty scope stayed active).

### T-18 — Fix every other test the leak check fails in tests/pool and tests/agent
- Acceptance: uv run pytest tests/pool tests/agent passes with the leak check on (seen leaking on 2026-09-28: four tests in test_reload, three more in test_waiting, test_start_raises_the_wall in test_runner, the holders-stop test in test_console_lease), and afterwards no process has a /tmp/pytest-of-<user> path and no specflo-member scope is active
- Verify: uv run pytest tests/pool tests/agent; ps -eo args | grep pytest-of-; systemctl --user list-units --all --no-legend 'specflo-member-*'
- Implements: REQ-12
- Depends on: T-17
- Files: tests/pool/test_reload.py, tests/pool/test_waiting.py, tests/pool/test_runner.py, tests/pool/test_lease_request.py, tests/pool/test_console_lease.py
- Scope: Large
- Milestone: M-04
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] With the rig cleanup change no listed test leaks: three runs of tests/pool tests/agent (1572 passed, 5 skipped) left no pytest-of process, no member scope and no stub_pi, socat or bwrap; the only edit moves three wait texts into message=.

### T-19 — Give directory rights back in the piconfig remove test's own teardown, closing FU-88
- Acceptance: With a planted failure right after its chmod, the test's directories are left with owner rwx; after a run no garbage-* directory is in the pytest temp root
- Verify: uv run pytest tests/pool/test_piconfig_remove.py; ls -d /tmp/pytest-of-$USER/garbage-*
- Implements: REQ-13
- Files: tests/pool/test_piconfig_remove.py
- Scope: Small
- Milestone: M-04
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] A take_rights fixture records each chmod and gives owner rwx back in teardown; a planted failure after the chmod left 700 on both directories and no garbage dir

### T-20 — Fix and unskip test_accounts_402 'the first agent', closing FU-85
- Acceptance: The case has no skip marker, and it passes 10 runs in a row alone and in a parallel run of tests/pool; the fix is at the root cause (the watcher or the test's wait), recorded in a task note
- Verify: for i in $(seq 10); do uv run pytest -n 0 -q 'tests/pool/test_accounts_402.py' -k 'first' || break; done; uv run pytest tests/pool
- Implements: REQ-17, REQ-18
- Depends on: T-18
- Files: tests/pool/test_accounts_402.py, src/specflo/pool/watch.py
- Scope: Medium
- Milestone: M-05
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Fault in the test, not the watcher: two leases share one scenario file and the grant returns before the pi reads it, so the first pi read the second lease's scenario (no refusal) and the watcher correctly saw nothing to retry. watch.py is unchanged.

### T-21 — Fix and unskip the two ledger capacity tests, closing FU-86 and FU-93
- Acceptance: Neither test has a skip marker; each waits for the request it means, and both pass 10 runs in a row alone and in a parallel run of tests/pool
- Verify: for i in $(seq 10); do uv run pytest -n 0 -q tests/pool/test_ledger_capacity.py || break; done; uv run pytest tests/pool
- Implements: REQ-17, REQ-18
- Depends on: T-18
- Files: tests/pool/test_ledger_capacity.py
- Scope: Medium
- Milestone: M-05
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Root cause: a grant returns before its pi writes the launch record, so the test read the first lease's record. The lease always went to the right request. Proven with a 2 s planted recorder delay on the second launch.

### T-22 — Make the web project serving test wait for the agent to settle, closing FU-89
- Acceptance: test_the_control_starts_the_agent_and_the_page_then_shows_it_serving waits for idle through the shared helper, and passes 10 runs in a row alone and in a whole-suite parallel run
- Verify: for i in $(seq 10); do uv run pytest -n 0 -q tests/test_web_project.py -k serving || break; done
- Implements: REQ-18
- Depends on: T-18
- Files: tests/test_web_project.py
- Scope: Small
- Milestone: M-05
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Root cause: the start returns once the agent takes its opening prompt; the pump writes idle when the run settles, late under load. Proven with a planted 1 s settle delay. Two sibling tests in the same file (out-of-band restart, fresh daemon discovery) had the same race, proven the same way, and take the same one-line fix.

### T-23 — Make the console lease holders-stop test wait without a fixed limit that load exceeds, closing FU-102
- Acceptance: The holders-stop test waits through the shared helper, and passes 10 runs in a row alone and in a parallel run of tests/pool
- Verify: for i in $(seq 10); do uv run pytest -n 0 -q tests/pool/test_console_lease.py -k holders_stop || break; done; uv run pytest tests/pool
- Implements: REQ-18
- Depends on: T-18
- Files: tests/pool/test_console_lease.py
- Scope: Small
- Milestone: M-05
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Root cause: a stop sent while bwrap was still making the sandbox ended only the outer bwrap; the host then waited 5 s twice for pi's output to close (about 10 s), which load pushed past the fixed 30 s CLI default. The product race stays open as a follow-up.

### T-24 — Fix and unskip the scope sweep test
- Acceptance: The test has no skip marker, its leaked-scope fixture tears down what it made within the shared helper's limits, and it passes 10 runs in a row alone and in a parallel run of tests/pool
- Verify: for i in $(seq 10); do uv run pytest -n 0 -q tests/pool/test_scope_sweep.py || break; done; uv run pytest tests/pool
- Implements: REQ-17, REQ-18
- Depends on: T-12, T-18
- Files: tests/pool/test_scope_sweep.py
- Scope: Small
- Milestone: M-05
- Progress: done
- Status: active
- Note: 2026-09-28 [Note] Skipped as slow: the random kill race leaked rarely, and other tests' sweeps stopped the leak first. An unreaped exited pid makes an empty active scope every time; a test-only scope prefix keeps other sweeps off it. Its scopes are named specflo-membertest*, outside the specflo-member-* check pattern.

### T-25 — Describe the parallel suite and test_command in the README and CHANGELOG
- Acceptance: README says uv run pytest runs on 8 workers, -n 0 turns that off for -s or pdb, and SPECFLO_TEST_WAIT_SCALE scales test waits; README and CHANGELOG describe test_command in the hints, the reviewer brief and the skills; both files stay ASCII
- Verify: uv run pytest tests/test_docs_ascii.py
- Implements: REQ-22, REQ-05, REQ-06, REQ-07
- Depends on: T-02, T-05, T-06, T-07
- Files: README.md, CHANGELOG.md
- Scope: Small
- Milestone: M-05
- Progress: done
- Status: active

### T-26 — Prove the whole suite: three parallel runs within 180 s that leave nothing behind
- Acceptance: Three runs of uv run pytest in a row each exit 0 with 0 failed and 0 errors within 180 s; the -rs report lists only the off-host provider skip; after each run no process has a /tmp/pytest-of-<user> path, no specflo-member scope is active and no garbage-* directory exists
- Verify: for i in 1 2 3; do time uv run pytest || break; ps -eo args | grep -c pytest-of-; systemctl --user list-units --all --no-legend 'specflo-member-*'; ls -d /tmp/pytest-of-$USER/garbage-*; done
- Implements: REQ-03, REQ-12, REQ-18
- Depends on: T-03, T-08, T-15, T-19, T-20, T-21, T-22, T-23, T-24, T-25
- Scope: Small
- Milestone: M-05
- Progress: done
- Status: active
- Note: 2026-09-28 [Design] FU-61 names two tests (FU-85 in T-20 and FU-89 in T-22); close it here, once both pass in the three whole-suite runs.
- Note: 2026-09-28 [Note] Three runs of uv run pytest: 3916 passed, 1 skipped (off-host provider only) in 129 s, 110 s and 110 s; after each no pytest-of process, no specflo-member* unit and no garbage dir.

## Open questions

- T-20: whether the fault of test_accounts_402 'the first agent' is in the test or in the watcher (FU-85 says it fails 3 of 3 runs alone). The task note records which, and a watcher fault is fixed in src/specflo/pool/watch.py.
- T-18: the list of leaking tests is from one scan on 2026-09-28; the leak check may find more.

## Canonical refs

- docs/projects/suite-cost/spec.md
- docs/projects/suite-cost/brainstorm.md
- tests/conftest.py, tests/pool/conftest.py, tests/pool/test_runner.py (Rig, wait_until, cleanup)
- src/specflo/pool/launch.py (member_env allowlist), src/specflo/pool/sandbox.py (scope and bwrap argv)
- src/specflo/workflow.py (_review_hint), src/specflo/review.py (reviewer brief)
- Agent Wiki: research/pytest-suites-with-real-subprocesses-speed-test-selection-leaks-and-load-flakes-2026-09.md
