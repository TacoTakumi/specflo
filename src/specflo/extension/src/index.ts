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
import * as fs from "node:fs";
import * as path from "node:path";
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
 * One rendered status segment: the uncolored text and the theme-token style
 * the wiring applies it with.
 *
 * Mirrors statusline.sh's specflo_seg output minus the ANSI: the wiring owns
 * colour, via ctx.ui.theme, so the segment follows the active pi theme and no
 * escape byte lives in this source (REQ-07).
 */
export interface SegmentText {
  text: string;
  style: "accent" | "dim";
}

/**
 * The specflo status segment for the session cwd, or null when there is
 * nothing to show.
 *
 * Parses the specflo artifacts directly - .specflo/config.yaml walked up from
 * ``cwd``, the active project's project.md frontmatter, and its plan.md task
 * blocks - with exactly the rules of ~/.claude/statusline.sh's specflo_seg
 * (D-02). A slug longer than 17 characters is truncated to its first 16 plus
 * an ellipsis; complete/shelved render dim 'slug done'/'slug shelved'; plan
 * and execute append a task tally ' T-NN d/total' counting non-superseded
 * tasks only (an explicit `Superseded by:` field or the legacy `Status:
 * superseded by` phrase marks a task superseded), and execute drops the phase
 * label to buy back width. Anything missing or unparseable - no config.yaml
 * on the walk, no active_project, a broken frontmatter, an unreadable
 * plan.md - yields null, so the caller clears the status. Never throws: the
 * whole walk is guarded and a throwing read degrades to null (REQ-02).
 */
export function computeSegment(cwd: string): SegmentText | null {
  try {
    // Walk up from the cwd to the first .specflo/config.yaml, exactly the
    // statusline's loop (realpath first, so a symlinked cwd resolves the
    // same repo a shell would resolve it to). SPECFLO_DIRECTORY, when set and
    // non-empty, is where the walk starts instead - the same override the
    // specflo CLI honours - so the segment names the tree specflo acts on. A
    // missing directory fails realpath and lands in the catch below: no
    // segment, never a throw.
    const override = process.env.SPECFLO_DIRECTORY;
    let root = fs.realpathSync(override ? override : cwd);
    for (;;) {
      if (fs.existsSync(path.join(root, ".specflo", "config.yaml"))) break;
      const parent = path.dirname(root);
      if (parent === root) return null;
      root = parent;
    }
    // config.yaml: only the two keys the segment needs, comment lines and
    // anything else ignored, values unquoted.
    const cfg: Record<string, string> = {};
    for (const line of fs.readFileSync(path.join(root, ".specflo", "config.yaml"), "utf8").split("\n")) {
      const m = /^(projects_dir|active_project):\s*(.+?)\s*$/.exec(line);
      if (m !== null) cfg[m[1]] = m[2].trim().replace(/^['"]+|['"]+$/g, "");
    }
    const slug = cfg["active_project"];
    if (!slug) return null;
    // Code-point length, as Python's len; ASCII slugs are identical either way.
    const label = [...slug].length <= 17 ? slug : [...slug].slice(0, 16).join("") + "\u2026";
    const pdir = path.join(root, cfg["projects_dir"] ?? "docs/projects", slug);
    // project.md: the frontmatter block alone, first 2048 characters, with
    // the last phase/status match winning (dict semantics in the statusline).
    let head: string;
    try {
      head = fs.readFileSync(path.join(pdir, "project.md"), "utf8").slice(0, 2048);
    } catch {
      return null;
    }
    const fm = /^---\r?\n([\s\S]*?)\r?\n---/.exec(head);
    if (fm === null) return null;
    const meta: Record<string, string> = {};
    for (const m of fm[1].matchAll(/^(phase|status):\s*['"]?([\w-]+)/gm)) meta[m[1]] = m[2];
    const phase = meta["phase"];
    if (phase === undefined) return null;
    if (meta["status"] === "complete") return { text: `${label} done`, style: "dim" };
    if (meta["status"] === "shelved") return { text: `${label} shelved`, style: "dim" };
    let seg = `${label}:${phase}`;
    // Task tally for plan/execute: `### T-NN` blocks carrying machine-managed
    // `- Progress:` / `- Status:` / `- Superseded by:` fields.
    if (phase === "plan" || phase === "execute") {
      const tasks: Array<{ id: string; progress: string; sup: boolean }> = [];
      let cur: (typeof tasks)[number] | null = null;
      try {
        for (const line of fs.readFileSync(path.join(pdir, "plan.md"), "utf8").split("\n")) {
          const t = /^### (T-\d+)/.exec(line);
          if (t !== null) {
            cur = { id: t[1], progress: "pending", sup: false };
            tasks.push(cur);
          } else if (line.startsWith("### ")) {
            cur = null;
          } else if (cur !== null) {
            const f = /^- (Progress|Status|Superseded by):\s*(.+?)\s*$/.exec(line);
            if (f !== null) {
              if (f[1] === "Progress") cur.progress = f[2];
              else if (f[1] === "Superseded by" || f[2].includes("superseded by")) cur.sup = true;
            }
          }
        }
      } catch {
        // An unreadable plan.md leaves the tally off, as in the statusline.
      }
      const active = tasks.filter((t) => !t.sup);
      if (active.length > 0) {
        const wip = active.find((t) => t.progress === "in_progress");
        const done = active.filter((t) => t.progress === "done").length;
        const tail = `${wip !== undefined ? ` ${wip.id}` : ""} ${done}/${active.length}`;
        // In execute the tally already implies the phase; keep the label only.
        seg = phase === "execute" ? label + tail : seg + tail;
      }
    }
    return { text: seg, style: "accent" };
  } catch {
    return null;
  }
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

  // The status segment's session-local memory: the last rendered value the
  // segment was applied with, so a turn_end refresh can gate on change. It
  // dies with the session like the other closure state, which is fine -
  // session_start re-applies unconditionally, and that re-application is what
  // restores the segment after pi wipes extension status on session
  // invalidation or /reload (REQ-05). null means never applied this session;
  // undefined means applied as a clear.
  let lastStatusText: string | undefined | null = null;

  /**
   * Apply the specflo status segment to the footer.
   *
   * Computes the segment from the session cwd and renders it through the
   * theme tokens - magenta for the active segment, dim for complete/shelved -
   * with no escape byte of its own (REQ-07), then calls ctx.ui.setStatus with
   * the key 'specflo': the segment's single setStatus call site (REQ-04).
   * ``force`` is true on session_start, where pi may have wiped the status
   * and the segment must be re-applied whatever it held before (REQ-05); on
   * turn_end it is false and the set is gated on the rendered text actually
   * changing (REQ-06). The colors are theme tokens - accent for the active
   * segment (pi's highlight token; the theme set has no magenta, D-07), dim
   * for complete/shelved - with no escape byte of its own (REQ-07). The
   * theme is present in TUI mode and absent on the
   * RPC-mode ui context, so it is reached through optional chaining: color
   * comes from ctx.ui.theme when theming exists, else the text is sent as-is
   * for the RPC host to render (the same setStatus call informs RPC clients,
   * D-03). Nothing to show - no specflo repo on the cwd walk, no
   * active project, unparseable artifacts, or a throwing compute - renders
   * undefined, which clears the status (REQ-02); a throw inside the compute
   * degrades to that clear, so no exception escapes this function.
   */
  function applySegment(ctx: ExtensionContext, force: boolean): void {
    let computed: SegmentText | null;
    try {
      computed = computeSegment(ctx.cwd);
    } catch {
      computed = null;
    }
    const rendered =
      computed === null
        ? undefined
        : (ctx.ui.theme?.fg(computed.style, computed.text) ?? computed.text);
    if (force || rendered !== lastStatusText) {
      ctx.ui.setStatus("specflo", rendered);
      lastStatusText = rendered;
    }
  }

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

  // The status segment, wired through its own listeners so the armed-seam
  // path above stays untouched: re-applied on every session start whatever
  // the reason (startup, resume, new, fork, reload - REQ-05), refreshed on
  // turn_end only when the rendered text changed (REQ-06).
  pi.on("session_start", (_event, ctx) => {
    applySegment(ctx, true);
  });
  pi.on("turn_end", (_event, ctx) => {
    applySegment(ctx, false);
  });
}
