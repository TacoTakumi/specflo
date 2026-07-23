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
 * Whether context usage has reached the arming threshold.
 *
 * The comparison is on the percent of the context window alone (REQ-04): no
 * token count enters here, so no absolute token constant can. Unknown usage -
 * an undefined reading, or a null percent as compaction leaves it - never arms
 * (REQ-05), and neither does an unknown threshold.
 */
function isArmed(threshold: number | null, usage: ContextUsage | undefined): boolean {
  if (threshold === null) return false;
  const percent = usage?.percent;
  if (typeof percent !== "number" || !Number.isFinite(percent)) return false;
  return percent >= threshold;
}

export default function specflo(pi: ExtensionAPI): void {
  // All the state this extension holds, and it dies with the process: the
  // payload fetched at session start waiting for a turn to carry it, and the
  // arming threshold seeded from the cold-start status snapshot.
  let pendingReseed: string | null = null;
  let threshold: number | null = null;

  pi.on("session_start", async (event: SessionStartEvent, ctx: ExtensionContext) => {
    if (!COLD_START_REASONS.has(event.reason)) return;
    // No direct-continuation flag: a cold start has no one's answer yet about
    // whether to keep going, which is exactly what the ask-first payload is for.
    const payload = await runSpecflo(["hook", "reseed"], ctx.cwd);
    // Outside a specflo project, or with no active one, the command prints
    // nothing - and nothing is what the session should be told.
    pendingReseed = payload ? payload : null;
    // Seed the arming threshold from the same status snapshot seam detection
    // will read, so the per-turn arming check stays a subprocess-free in-process
    // read (REQ-26). It persists in this closure across a later on-demand clear's
    // `new` session, which is why that path fetches nothing of its own.
    threshold = readThreshold(await runSpecflo(["status", "--json"], ctx.cwd));
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
    if (!isArmed(threshold, ctx.getContextUsage())) return;
    // Armed: take the seam poll. Reading the snapshot is the only subprocess an
    // armed turn spawns; declaring a seam from it, and acting on that seam, is
    // the next task's work.
    await runSpecflo(["status", "--json"], ctx.cwd);
  });
}
