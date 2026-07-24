/**
 * specflo - a thin pi extension that drives specflo's continuation loop.
 *
 * Everything this extension knows it learns by running the `specflo` binary and
 * relaying its stdout byte for byte. It opens no project artifact, keeps no
 * durable state, registers no model-callable tool, and blocks no tool call.
 *
 * The behaviour lands task by task:
 *   - cold start: fetch `specflo hook reseed` and inject it on the first turn
 *   - arming:     watch context usage against the threshold `status --json` reports
 *   - seam:       poll `status --json` while armed for a phase or task change
 *   - on demand:  a /specflo-continue command clears and reseeds now, armed or not
 *   - attended:   one passive notice per seam, via ctx.ui.notify only
 *   - unattended: at a seam while an auto run is under way, end the running
 *     agent and clear and reseed through the live anchor - the replacement
 *     context the previous clear captured - or, unanchored, notice the
 *     command that starts the chain
 *
 * This file is the single extension entry point named by package.json's
 * `pi.extensions`.
 */

import { execFile } from "node:child_process";
import type {
  BeforeAgentStartEventResult,
  ContextUsage,
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
  SessionStartEvent,
  TurnEndEvent,
} from "@earendil-works/pi-coding-agent";

/**
 * The binary every piece of specflo state is read through.
 *
 * Resolved per call rather than captured at load, so an environment settled
 * after pi started - or between two runs in the same process - is honoured.
 */
function specfloBin(): string {
  return process.env.SPECFLO_BIN ?? "specflo";
}

/**
 * Session starts that mean "a session has just come up cold".
 *
 * `new` and `fork` are deliberately absent: those sessions are opened by
 * something that already knows what it wants next - the on-demand clear carries
 * its own payload - and `reload` is a resource refresh, not a new session.
 */
const COLD_START_REASONS: ReadonlySet<SessionStartEvent["reason"]> = new Set([
  "startup",
  "resume",
]);

/** Marks the injected message in the session log. */
const RESEED_MESSAGE_TYPE = "specflo-reseed";

/** The slash command that clears the session and reseeds on demand. */
const CONTINUE_COMMAND = "specflo-continue";

/**
 * The command argument selecting the unattended auto-run continuation.
 *
 * `/specflo-continue auto` fetches the `specflo auto` pass report instead of
 * the on-demand payload - the same explicit opt-in the `specflo auto` command
 * itself is. It is also what the bootstrap notice tells the user to type at an
 * unanchored auto-run seam (REQ-31): the clear it performs anchors the chain,
 * and every seam after it fires unattended.
 */
const AUTO_ARGUMENT = "auto";

/**
 * The live anchor: the replacement-session context captured at the last clear,
 * and the one piece of cross-session memory the extension holds (REQ-30).
 *
 * It exists because pi gives event contexts no `newSession` and dispatches no
 * extension-queued slash command, so an armed seam can only clear through a
 * command-capable context captured earlier (REQ-29). Module state, not closure
 * state: pi rebuilds the extension closure for every session, and the anchor
 * must outlive the session whose clear captured it. It is a pi handle, never
 * specflo state, and it dies with the process - nothing durable (REQ-30). It
 * goes stale when something other than the chain replaces the session (a user
 * `/reload` or manual switch); the fire treats a throw as staleness, drops the
 * anchor, and the next armed auto seam degrades to the bootstrap notice.
 */
let liveAnchor: ExtensionCommandContext | null = null;

/**
 * Latch held while a fire is crossing waitForIdle: a second seam declared
 * before the first clear lands must not open a second session.
 */
let fireInFlight = false;

/** Test hook: forget the anchor and the latch between unit cases. */
export function resetChainForTests(): void {
  liveAnchor = null;
  fireInFlight = false;
}

/**
 * What the user is told when /specflo-continue has nothing to continue.
 *
 * The command's own prose, so it reaches ctx.ui and never model context
 * (REQ-27). Shown when `hook reseed --continue` yields nothing - no active
 * project, or no specflo here at all - which is also why nothing is cleared.
 */
const NOTHING_TO_CONTINUE = "specflo has no active project to continue here.";

/** Generous ceiling for a reseed payload; a checkpoint plus a task brief is small. */
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;

/**
 * Run a specflo command and return its stdout verbatim, or null if it failed.
 *
 * Never throws and never rejects: a missing binary, a non-zero exit or a
 * directory that is not a specflo repo all mean "specflo has nothing to say
 * here", which must leave the pi session exactly as it was.
 */
function runSpecflo(args: string[], cwd: string): Promise<string | null> {
  return new Promise((resolve) => {
    execFile(
      specfloBin(),
      args,
      { cwd, encoding: "utf8", maxBuffer: MAX_OUTPUT_BYTES },
      (error, stdout) => resolve(error ? null : stdout),
    );
  });
}

/**
 * Replace the current session and deliver ``payload`` into the new one.
 *
 * The clear is `ctx.newSession`, the extension's own, owing nothing to
 * pi-clearthen (REQ-29); the payload crosses into the new session as the CLI's
 * text verbatim, one message that runs one turn (REQ-27). Every clear anchors
 * the chain: the replacement context is the only command-capable handle the
 * next armed seam can fire through (REQ-29, REQ-31).
 *
 * The reseed is *dispatched, not awaited*, and that is load-bearing.
 * `sendMessage(..., {triggerTurn: true})` settles only when the run that
 * message starts ends - minutes, in a real auto pass. Awaiting it held
 * `ctx.newSession` open for that whole run, so the fire's `finally` never
 * cleared `fireInFlight` in time and every seam the reseeded run declared was
 * latched out: the chain fired exactly once (pi-fixes-2 REQ-01, REQ-02).
 * Returning at delivery keeps the latch to milliseconds and holds no
 * `newSession` frame for the child run's life (REQ-05). A failed dispatch
 * drops the anchor, so the next armed auto seam degrades to the bootstrap
 * notice instead of firing through a session that never received anything
 * (REQ-04).
 */
async function reseedInto(ctx: ExtensionCommandContext, payload: string): Promise<void> {
  await ctx.newSession({
    withSession: async (session) => {
      liveAnchor = session;
      void session
        .sendMessage(
          // Verbatim: the CLI's text, unedited and untemplated.
          { customType: RESEED_MESSAGE_TYPE, content: payload, display: false },
          { triggerTurn: true },
        )
        .catch(() => {
          // Only this callback's own anchor: a later clear may already have
          // re-anchored the chain, and that one is live.
          if (liveAnchor === session) liveAnchor = null;
        });
    },
  });
}

/**
 * Clear the current session and reseed the active project's continuation
 * payload into the replacement, or say why it did not.
 *
 * The payload is `hook reseed --continue`: the direct-continuation form (REQ-22),
 * because a caller reaching here has already decided to keep going, so the
 * ask-first gate is moot. It is fetched before any clear, so a run with no
 * active project - the CLI prints nothing - discards nothing (REQ-11).
 */
async function clearAndContinue(ctx: ExtensionCommandContext): Promise<void> {
  const payload = await runSpecflo(["hook", "reseed", "--continue"], ctx.cwd);
  if (!payload) {
    // Nothing to continue: leave the session untouched and tell the user why.
    ctx.ui.notify(NOTHING_TO_CONTINUE, "warning");
    return;
  }
  await reseedInto(ctx, payload);
}

/**
 * One `specflo auto --json` pass report, or null when the CLI produced none.
 *
 * ``payload`` is the pass's directive text and ``stop`` the CLI's verdict on
 * whether the loop ends here - decided entirely CLI-side, which is why nothing
 * else is read (REQ-13). A report that never arrived or does not parse reads as
 * null: nothing to deliver, so nothing fires.
 */
function parseAutoReport(stdout: string | null): { payload: string; stop: boolean } | null {
  if (stdout === null) return null;
  try {
    const obj = JSON.parse(stdout) as { payload?: unknown; stop?: unknown };
    if (typeof obj.payload !== "string") return null;
    return { payload: obj.payload, stop: obj.stop === true };
  } catch {
    return null;
  }
}

/**
 * Continue an auto run unattended: fetch one pass report and act on its
 * verdict - clear and reseed on a continuable pass, halt on a stop (REQ-31).
 *
 * `specflo auto --json` is the run's single authority: it holds the pass
 * counter, the cap, the kill switch, the stall detector and the completion
 * check, so this handler reads one verdict and counts nothing (REQ-13). On
 * stop there is no clear and no further report - the CLI has already marked
 * the run ended - and its stop directive reaches the user via ctx.ui alone,
 * the CLI's own words, never model context. An empty or unparseable report
 * has nothing to deliver and fires nothing.
 */
async function autoContinue(ctx: ExtensionCommandContext): Promise<void> {
  const report = parseAutoReport(await runSpecflo(["auto", "--json"], ctx.cwd));
  if (report === null || report.payload === "") return;
  if (report.stop) {
    ctx.ui.notify(report.payload, "warning");
    return;
  }
  await reseedInto(ctx, report.payload);
}

/**
 * The arming threshold read from a `status --json` snapshot, or null.
 *
 * The CLI owns the default and the config key (REQ-28); the extension only reads
 * the resolved percent it reports and never parses the file. An absent field, a
 * non-number, or a snapshot that never arrived all read as null - an unknown
 * threshold, which never arms.
 */
function readThreshold(statusJson: string | null): number | null {
  if (statusJson === null) return null;
  try {
    const value = (JSON.parse(statusJson) as { context_threshold_percent?: unknown })
      .context_threshold_percent;
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  } catch {
    return null;
  }
}

/**
 * The context-usage percent when it has reached the arming threshold, or null.
 *
 * The comparison is on the percent of the context window alone (REQ-04): no
 * token count enters here, so no absolute token constant can. Unknown usage -
 * an undefined reading, or a null percent as compaction leaves it - never arms
 * (REQ-05), and neither does an unknown threshold. The armed percent is
 * returned rather than a boolean because the attended notice names it.
 */
function armedPercent(threshold: number | null, usage: ContextUsage | undefined): number | null {
  if (threshold === null) return null;
  const percent = usage?.percent;
  if (typeof percent !== "number" || !Number.isFinite(percent)) return null;
  return percent >= threshold ? percent : null;
}

/**
 * Whether ``statusJson`` reports an auto run under way.
 *
 * The CLI decides and reports this (REQ-13); the extension only reads the
 * flag. Anything short of an explicit true - an absent block, a failed poll,
 * an older CLI without the field - reads as attended, the mode that only ever
 * notifies and never clears.
 */
function readAutoUnderWay(statusJson: string | null): boolean {
  if (statusJson === null) return false;
  try {
    const value = (JSON.parse(statusJson) as { auto_run?: { under_way?: unknown } }).auto_run
      ?.under_way;
    return value === true;
  } catch {
    return false;
  }
}

/**
 * The two fields of a status snapshot seam detection compares: the phase, and
 * how many tasks are done. Both move only at a safe point - a phase completes,
 * or a task reaches done - so a change in either is a seam, and a change in
 * anything else (a task starting, a checkpoint rewritten) is not.
 */
export interface StatusSnapshot {
  phase: string | null;
  done: number | null;
}

/**
 * Extract the seam signature from a status --json snapshot, or null.
 *
 * A snapshot that never arrived (a failed poll, no active project) or does not
 * parse reads as null - an absence, which declares no seam and disturbs no
 * baseline. A missing or wrong-typed field reads as null for that field alone.
 */
export function parseSnapshot(statusJson: string | null): StatusSnapshot | null {
  if (statusJson === null) return null;
  try {
    const obj = JSON.parse(statusJson) as {
      phase?: unknown;
      progress?: { done?: unknown };
    };
    const done = obj.progress?.done;
    return {
      phase: typeof obj.phase === "string" ? obj.phase : null,
      done: typeof done === "number" && Number.isFinite(done) ? done : null,
    };
  } catch {
    return null;
  }
}

/**
 * What makes ``current`` a seam relative to the last observed snapshot, as a
 * short human phrase for the attended notice - or null when it is no seam.
 *
 * A seam is a phase change or a task reaching done (REQ-07). A task merely
 * moving to in_progress leaves both fields unchanged and so declares nothing,
 * which is exactly why no in-flight task is ever discarded (REQ-08). Only an
 * increase in the done count counts - a task un-done or removed is not a task
 * reaching done.
 */
export function describeSeam(last: StatusSnapshot, current: StatusSnapshot): string | null {
  if (current.phase !== last.phase) {
    return current.phase === null ? "the phase changed" : `the phase is now ${current.phase}`;
  }
  if (
    typeof current.done === "number" &&
    typeof last.done === "number" &&
    current.done > last.done
  ) {
    return `a task reached done (${current.done} done)`;
  }
  return null;
}

/**
 * Whether ``current`` is a seam relative to the last observed snapshot: a safe
 * point to clear because nothing is in flight.
 */
export function isSeam(last: StatusSnapshot, current: StatusSnapshot): boolean {
  return describeSeam(last, current) !== null;
}

/**
 * The notice for an armed seam: the current context usage, the seam that
 * fired, and the exact command to run - extension-authored prose, and it
 * reaches ctx.ui.notify alone, never model context (REQ-27). The attended
 * notice (REQ-09) names `/specflo-continue`; the bootstrap notice at an
 * unanchored auto-run seam (REQ-31) names `/specflo-continue auto`.
 */
function noticeText(percent: number, seam: string, command: string): string {
  return (
    `specflo: context is at ${Math.round(percent)}% and ${seam} - a safe point to clear. ` +
    `Run ${command} to keep going in a fresh session.`
  );
}

export default function specflo(pi: ExtensionAPI): void {
  // The session-local state, and it dies with the session: pi builds a fresh
  // closure from this factory for every session, so nothing here survives a
  // clear (the live anchor lives above, at module level, for exactly that
  // reason). The payload fetched at session start waits for a turn to carry
  // it; the arming threshold and the last observed status snapshot - the one
  // seam detection compares each armed poll against - are the session-local
  // memory REQ-30 allows, and both seed from each session's one status
  // snapshot.
  let pendingReseed: string | null = null;
  let threshold: number | null = null;
  let lastSnapshot: StatusSnapshot | null = null;

  // The user-reachable clear-and-reseed entry point: plain for the on-demand
  // continue, `auto` for continuing an auto run - which also anchors the chain
  // the unattended fire needs (REQ-31). It is no tool - the model cannot call
  // it - and holds no state of its own.
  pi.registerCommand(CONTINUE_COMMAND, {
    description: "Clear the session and reseed the active specflo project to keep going.",
    handler: (args: string, ctx: ExtensionCommandContext) =>
      args.trim() === AUTO_ARGUMENT ? autoContinue(ctx) : clearAndContinue(ctx),
  });

  pi.on("session_start", async (event: SessionStartEvent, ctx: ExtensionContext) => {
    if (COLD_START_REASONS.has(event.reason)) {
      // No direct-continuation flag: a cold start has no one's answer yet about
      // whether to keep going, which is exactly what the ask-first payload is
      // for. A `new` session gets no payload at all: whoever opened it - the
      // on-demand clear, the unattended fire - already delivered its own.
      const payload = await runSpecflo(["hook", "reseed"], ctx.cwd);
      // Outside a specflo project, or with no active one, the command prints
      // nothing - and nothing is what the session should be told.
      pendingReseed = payload ? payload : null;
    }
    // One status snapshot seeds both the arming threshold and the seam-detection
    // baseline, so the per-turn arming check stays a subprocess-free in-process
    // read (REQ-26) and the first armed poll has something to compare against.
    // Every session seeds its own, whatever the start reason: this closure is
    // per-session, so the `new` session a clear opens starts blank - without
    // this it would never re-arm, and the auto loop would fire exactly once.
    const statusJson = await runSpecflo(["status", "--json"], ctx.cwd);
    threshold = readThreshold(statusJson);
    lastSnapshot = parseSnapshot(statusJson);
  });

  pi.on("before_agent_start", (): BeforeAgentStartEventResult | undefined => {
    // session_start has no injection result to return and before_agent_start
    // does, so the payload waits here for the first turn of the session.
    if (pendingReseed === null) return undefined;
    const content = pendingReseed;
    pendingReseed = null; // one injection per cold start, not one per turn
    return {
      message: {
        customType: RESEED_MESSAGE_TYPE,
        // Verbatim: the CLI's stdout, unedited and untemplated.
        content,
        display: false,
      },
    };
  });

  pi.on("turn_end", async (_event: TurnEndEvent, ctx: ExtensionContext) => {
    // The arming check is this line alone: an in-process read of context usage
    // against the seeded threshold, spawning nothing (REQ-26). Unknown usage and
    // an unknown threshold both leave it unarmed (REQ-05).
    const percent = armedPercent(threshold, ctx.getContextUsage());
    if (percent === null) return;
    // Armed: take the seam poll. Reading the snapshot is the only subprocess an
    // armed turn spawns.
    const statusJson = await runSpecflo(["status", "--json"], ctx.cwd);
    const current = parseSnapshot(statusJson);
    // A poll that yielded no parseable snapshot declares nothing and leaves the
    // baseline intact, so a transient failure never triggers a clear (REQ-08).
    if (current === null) return;
    // A seam is declared against the last observed snapshot; the observed one
    // then becomes the baseline the next armed poll compares against - which is
    // why the notice below fires once per seam, not once per turn (REQ-10).
    const seam = lastSnapshot === null ? null : describeSeam(lastSnapshot, current);
    lastSnapshot = current;
    if (seam === null) return;
    // A seam: a safe point to clear. Under an auto run it fires the unattended
    // continuation through the live anchor (REQ-31): pi gives this event
    // context no newSession and dispatches no extension-queued command, so the
    // clear runs through the command-capable context the previous clear
    // captured (REQ-29). Detached, because waitForIdle inside an awaited
    // turn_end handler would deadlock the very run it waits on; idle-gated,
    // because a clear must not rip the session out from under a running agent.
    if (readAutoUnderWay(statusJson)) {
      const anchor = liveAnchor;
      if (anchor === null) {
        // Unanchored - pi joined the run cold, or the anchor went stale. One
        // clear anchors the chain; the bootstrap notice names the command that
        // performs it (REQ-31), and it reaches ctx.ui alone (REQ-27).
        ctx.ui.notify(noticeText(percent, seam, `/${CONTINUE_COMMAND} ${AUTO_ARGUMENT}`), "info");
        return;
      }
      if (fireInFlight) return;
      fireInFlight = true;
      void (async () => {
        try {
          await anchor.waitForIdle();
          await autoContinue(anchor);
        } catch {
          // A throw here means the anchor is stale - something other than the
          // chain replaced or reloaded the session. Drop it (unless a clear
          // re-anchored meanwhile) and let the next armed auto seam fall back
          // to the bootstrap notice.
          if (liveAnchor === anchor) liveAnchor = null;
        } finally {
          fireInFlight = false;
        }
      })();
      // End the running agent: waitForIdle resolves only when the run stops,
      // so without this the parked fire lands at the run's natural end -
      // tasks later, the sail-on. Abort comes last (latch, park, abort): the
      // fire above is already holding at waitForIdle when the run settles,
      // and a second seam during a parked fire latched out above, so nothing
      // double-aborts or double-fires (REQ-01, REQ-06).
      ctx.abort();
      return;
    }
    // Attended: say so once, passively, and clear nothing (REQ-09). The notice
    // reaches ctx.ui alone - never model context.
    ctx.ui.notify(noticeText(percent, seam, `/${CONTINUE_COMMAND}`), "info");
  });
}
