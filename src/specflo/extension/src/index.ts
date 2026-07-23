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
 *   - unattended: clear and reseed at a seam while an auto run is under way
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
 * Clear the current session and reseed the active project's continuation
 * payload into the replacement, or say why it did not.
 *
 * The payload is `hook reseed --continue`: the direct-continuation form (REQ-22),
 * because a caller reaching here has already decided to keep going, so the
 * ask-first gate is moot. It is fetched before any clear, so a run with no
 * active project - the CLI prints nothing - discards nothing (REQ-11). The clear
 * is `ctx.newSession`, the extension's own, owing nothing to pi-clearthen
 * (REQ-14); the payload crosses into the new session as the CLI's stdout
 * verbatim, one message that runs one turn (REQ-27).
 */
async function clearAndContinue(ctx: ExtensionCommandContext): Promise<void> {
  const payload = await runSpecflo(["hook", "reseed", "--continue"], ctx.cwd);
  if (!payload) {
    // Nothing to continue: leave the session untouched and tell the user why.
    ctx.ui.notify(NOTHING_TO_CONTINUE, "warning");
    return;
  }
  await ctx.newSession({
    withSession: async (session) => {
      await session.sendMessage(
        // Verbatim: the CLI's stdout, unedited and untemplated.
        { customType: RESEED_MESSAGE_TYPE, content: payload, display: false },
        { triggerTurn: true },
      );
    },
  });
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
 * The attended notice for an armed seam (REQ-09): the current context usage,
 * the seam that fired, and the exact command to run - the one piece of
 * extension-authored prose, and it reaches ctx.ui.notify alone, never model
 * context (REQ-27).
 */
function noticeText(percent: number, seam: string): string {
  return (
    `specflo: context is at ${Math.round(percent)}% and ${seam} - a safe point to clear. ` +
    `Run /specflo-continue to keep going in a fresh session.`
  );
}

export default function specflo(pi: ExtensionAPI): void {
  // All the state this extension holds, and it dies with the process: the
  // payload fetched at session start waiting for a turn to carry it, the arming
  // threshold, and the last observed status snapshot - the one seam detection
  // compares each armed poll against. The last two are the only session state
  // REQ-02 allows, and both seed from the one cold-start status snapshot.
  let pendingReseed: string | null = null;
  let threshold: number | null = null;
  let lastSnapshot: StatusSnapshot | null = null;

  // The one clear-and-reseed entry point. The user reaches it by typing the
  // command; the unattended fire (T-14) will reach the same handler by queueing
  // this command as a followUp from event context (REQ-14). It is no tool - the
  // model cannot call it - and holds no state of its own.
  pi.registerCommand(CONTINUE_COMMAND, {
    description: "Clear the session and reseed the active specflo project to keep going.",
    handler: (_args: string, ctx: ExtensionCommandContext) => clearAndContinue(ctx),
  });

  pi.on("session_start", async (event: SessionStartEvent, ctx: ExtensionContext) => {
    if (!COLD_START_REASONS.has(event.reason)) return;
    // No direct-continuation flag: a cold start has no one's answer yet about
    // whether to keep going, which is exactly what the ask-first payload is for.
    const payload = await runSpecflo(["hook", "reseed"], ctx.cwd);
    // Outside a specflo project, or with no active one, the command prints
    // nothing - and nothing is what the session should be told.
    pendingReseed = payload ? payload : null;
    // One status snapshot seeds both the arming threshold and the seam-detection
    // baseline, so the per-turn arming check stays a subprocess-free in-process
    // read (REQ-26) and the first armed poll has something to compare against.
    // Both persist in this closure across a later on-demand clear's `new`
    // session, which is why that path fetches nothing of its own.
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
    // A seam: a safe point to clear. Under an auto run its delivery is the
    // unattended fire (T-14), not the notice's - nothing here yet.
    if (readAutoUnderWay(statusJson)) return;
    // Attended: say so once, passively, and clear nothing (REQ-09). The notice
    // reaches ctx.ui alone - never model context.
    ctx.ui.notify(noticeText(percent, seam), "info");
  });
}
