---
name: specflo-agent
description: Use when driving pi subagents through the `specflo agent` CLI - starting a headless pi worker, sending it prompts, monitoring it, and stopping it. Triggers include "start a pi agent", "delegate this to a pi subagent", "prompt the agent", or an orchestration plan that hands tasks to `specflo agent` workers. Do NOT use for the specflo pipeline phases (brainstorm/spec/plan/execute have their own skills) or for driving pi interactively yourself.
---

# agent

Drive headless pi subagents through the `specflo agent` CLI. The CLI does the
heavy lifting - a detached host owns each pi process, logs everything, and
answers on a per-agent socket; this skill is only the driving pattern. No
logic beyond the CLI: if you want something the verbs do not offer, that is a
specflo feature request, not a workaround to script.

## The verbs

- `specflo agent start <name> [--transport rpc|tui] [--cwd DIR] [--pi-cmd CMD] [--workspace ID] [--no-herdr] [--no-auto-answer]` -
  start an agent in DIR. Default transport `rpc`: a detached host running
  `pi --mode rpc`, headless (or `--no-herdr`) it degrades with one warning.
  `--transport tui`: a real interactive pi in a herdr pane (herdr required),
  served by the specflo extension inside the session - same verbs, same
  socket. Names are unique among live agents.
- `specflo agent status <name> [--json]` - live-checked state (a dead host
  reports `dead`, never stale state) plus pids, herdr ids, and state-dir
  paths under `--json`.
- `specflo agent list [--json]` - every known agent, live-checked, with
  transport (`rpc`/`tui`) and ownership (`managed`/`adopted`) per row. Dead
  sessions are marked dead, never attachable.
- `specflo agent prompt <name> "<text>" [--timeout S] [--no-wait] [--steer | --follow-up]` -
  send work; blocks until the run settles, then prints the final assistant
  text to stdout.
- `specflo agent wait <name> [--timeout S]` - block until the current run
  settles (exit 0 immediately when idle).
- `specflo agent last <name>` - the most recent final assistant text.
- `specflo agent log <name> [--follow]` - the agent's event log; `--follow`
  streams.
- `specflo agent stop <name> [--timeout S]` - stop follows ownership: an rpc
  agent gets the graceful host stop (abort, terminate, logs retained); a
  managed tui agent gets SIGTERM and the extension cleans up; an adopted
  session is never killed - stop detaches it (record removed, pi left
  running) and exits `13`.

## The blocking-prompt-as-background-task pattern

A blocking `prompt` can run for minutes on a real model. Never busy-poll and
never block your whole session on it:

1. Run `specflo agent prompt <name> "<text>" --timeout <bound>` as a
   **background task** in your harness.
2. Keep working; **act on the completion notification**, not on a poll loop.
3. On completion, the task's stdout IS the assistant's final reply and the
   exit code tells you what happened (below).
4. If you must check in mid-run, use `specflo agent status <name>` (cheap,
   live-checked) or `specflo agent log <name> --follow` in another background
   task - never a second `prompt`.

Prefer one outstanding run per agent. To redirect a running agent, use
`prompt --steer` (delivered mid-run) or `prompt --follow-up` (queued for
after settle) - a plain prompt at a busy agent is refused, nothing is
delivered.

## Exit codes

Uniform across every verb; branch on them, do not parse stderr:

- `0` - success / run settled (for `prompt`: stdout is the final reply).
- `10` - agent busy (working). Decide: wait for settle, or resend with
  `--steer` / `--follow-up`.
- `11` - wait timeout. The run is still going: `wait` again with a longer
  bound, or `stop` if it is stuck.
- `12` - host unreachable or unknown agent. Check `specflo agent list`;
  a dead agent's name is reusable via a fresh `start`.
- `13` - `stop` on an adopted session detached it: the record is gone and
  the pi you did not start is still running. Not a failure.
- `1` - generic usage error; read stderr.

## The TUI transport: shared sessions

`start --transport tui` runs a real interactive pi in a herdr pane; every
verb above drives it over the same socket. What changes is who else is
there: a human can be typing in the very session you are prompting.

- **Discovery and attach.** Any pi session with the specflo extension serves
  by default, including ones a person started by hand. `specflo agent list`
  shows them as `tui adopted`, named by cwd basename; attach with `status`,
  `prompt`, `wait`, `last`, `log` - no start needed.
- **Prompt/typed coexistence.** Socket prompts and typed input land in one
  transcript in order. A plain prompt at a streaming session is refused
  (exit `10`); `--steer` / `--follow-up` deliver into the running turn or
  queue behind it. Prefer follow-up when a human may be mid-thought.
- **Blocked-state monitoring.** A blocking UI dialog flips the record to
  `needs-attention` with the prompt kind and title in the event log, and
  herdr shows `blocked` for the pane. Watch for it via `status --json` or
  `log --follow`.
- **Answering dialogs by keystroke.** Find the pane via `herdr agent list`
  (the row's `pane_id`), then answer at the terminal level:
  `herdr pane send-keys <pane> enter` (or `up`/`down` then `enter`), and
  `herdr pane send-text <pane> "..."` for input dialogs. The close lands in
  the event log and the state resumes.

## Monitoring

- `specflo agent status <name> --json` is the cheap truth: lifecycle state
  (`starting`/`idle`/`working`/`needs-attention`/`exited`/`stopped`/`dead`),
  pids, herdr placement, last activity.
- `specflo agent log <name> --follow` streams the event log when you need
  the play-by-play; the herdr pane shows the same feed human-readably.
- `needs-attention` on an rpc agent means the dialog auto-answer policy hit
  its flood threshold and stopped answering - look at the log, then steer or
  stop. On a tui agent it means a blocking dialog is open right now - answer
  it by keystroke (above) or let the human at the keyboard take it.

## Stopping

Always `specflo agent stop <name>` when the work is done - it aborts any
in-flight run, shuts pi (and, for rpc, the host) down cleanly, releases the
herdr registration, and leaves the event log on disk for the post-mortem.
On an adopted session stop only detaches (exit `13`) - the human's pi is
never yours to kill. Killing pids by hand loses the graceful path; only
fall back to that when `stop` itself reports the host unreachable.
