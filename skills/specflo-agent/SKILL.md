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
  streams. Under a lease it begins where the lease was bound.
- `specflo agent reset <name>` - start a new pi session in the same process:
  the context is cleared, the process and its launch flags are kept. Exit 10
  while the agent is working.
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

## Leased pool members

On a hosted checkout (one with a registered daemon) you do not `start` a
pooled agent: you lease a member of the daemon's pool, drive it with the verbs
above, and give it back. In local mode there is no daemon and the lease verbs
are refused - start your own agents as before.

- `specflo lease request <pool> [--cwd DIR] [--idle-limit 30m] [--label TEXT] [--wait S] [--egress local|no-train|open] [--remote NAME] [--json]` -
  take one member. It prints the lease id and the agent name; the lease token
  goes to a token file under the checkout, where every agent verb finds it by
  itself. Then drive the member: `specflo agent prompt <agent> "<text>"`.
  The member starts in `--cwd`, by default your working directory. The token
  files and the remote's token are under the checkout's `.specflo`, and a
  member with bash or read that starts in the checkout can read them: for a
  member that is not trusted, pass a `--cwd` outside the checkout. That keeps
  them out of its working tree, and no further: such a member runs as the
  same user on the same host and can still open them by absolute path.
- `specflo lease release <lease> [--remote NAME] [--json]` - give the lease
  back, always, when the work is over or has failed. A lease that has ended
  already is reported as it ended.
- `specflo lease list [--json]` - the leases this checkout holds. The leases
  of another orchestrator are never shown.

**A request can wait.** A pool that is full makes the request wait, up to
`--wait` seconds (600 by default). It says so at once on stderr: the pool,
what is full, its place among the waiting requests, and the limit. With
`--json` that notice is one JSON object on stderr and stdout carries only the
result. `--wait 0` refuses a full pool at once. Interrupt the command to
cancel the request. So run `lease request` as a background task, like a
blocking prompt, or pass a `--wait` you can live with.

**Some requests are refused at once and never wait.** `--egress` is the most
open class of member you accept; the default is `no-train`, and the pool's
definition and the hosted project's pin can only make it stricter. A pool
with no member under that ceiling is refused at once. When every such member
runs through a closed account, the refusal names the account and its reopen
time - request again after it.

**The team form.** `specflo lease request --team <name>` takes no `<pool>`
(exactly one of the two). A team is granted all or nothing and holds nothing
while it waits. It prints one team lease id and one agent per role member,
each with its own token file. `specflo lease release <team lease id>` ends
every member; a release of one member lease of a team exits non-zero and
names the team lease id. The orchestrator leads the team: the pool gives the
members no way to message each other, so what one member must learn from
another goes through your prompts.

**A leased member can be a developer's console.** On one, the holder's
`specflo agent stop` is refused with a non-zero exit: the process is the
developer's, so release the lease to be done with it. Prompt, wait, last, log
and reset work as on any member.

**Renewal is implicit.** Every verb you run on the member and every turn the
member works renews the lease; there is no renew verb. An idle lease expires
at its idle limit. `specflo agent reset <agent>` clears the member's context
on the same lease: the process, its directory and its model stay, and only
the lease holder may run it.

**When a lease has ended** and the member's host is gone, every agent verb
exits `12` and stderr names the cause (the one case where you read stderr):

- `Error: lease released` - you, or a team release, gave it back.
- `Error: lease expired` - it stayed idle past its idle limit.
- `Error: lease preempted by <request id>` - the pool took the member for
  another request.

In each case the member and its context are gone: request a lease again and
redo the task's prompt on the new member. Never retry the verb blindly on a
preempted lease - without a new request there is nothing to reach. A verb
with no token or the wrong token exits `1` with `agent '<name>' is leased to
another holder` and shows nothing of the member: run it from the checkout
that holds the token file, or pass `--lease-token` / set `SPECFLO_LEASE_TOKEN`.

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
