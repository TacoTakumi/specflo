/**
 * Shared rig for the unattended-fire unit suites (auto-fire.test.ts,
 * abort.test.ts): the canned status snapshots and pass reports, the
 * cold-start / armed-turn drivers, and the fake anchor chain.
 *
 * Lifted verbatim from auto-fire.test.ts so the abort coverage (T-03) drives
 * the same states through the same helpers instead of a parallel rig.
 */

import assert from "node:assert/strict";

import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  type FakeCtx,
  loadExtension,
} from "./fake-ctx.ts";

/** An auto payload with bytes an edit or a re-template would disturb. */
export const AUTO_PAYLOAD =
  "== specflo auto-mode bootstrap ==\nContinue the run.\n" +
  "# Checkpoint - demo\nline with trailing spaces   \n\tand a tab\n";

/**
 * A status --json snapshot shaped like the real one: the arming threshold, the
 * auto-run block, and the phase and done count seam detection compares.
 */
export function status(
  opts: { phase?: string; done?: number; autoUnderWay?: boolean } = {},
): string {
  const phase = opts.phase ?? "execute";
  const done = opts.done ?? 11;
  const total = 15;
  return JSON.stringify({
    phase,
    context_threshold_percent: 75,
    auto_run: { under_way: opts.autoUnderWay ?? false },
    progress: {
      total,
      by_state: { pending: total - done, in_progress: 0, done, blocked: 0 },
      done,
      all_done: done === total,
    },
  });
}

/** A `specflo auto --json` pass report. */
export function autoReport(
  opts: { payload?: string; stop?: boolean; reason?: string | null } = {},
): string {
  return JSON.stringify({
    payload: opts.payload ?? AUTO_PAYLOAD,
    stop: opts.stop ?? false,
    reason: opts.reason ?? null,
  });
}

/** Per-case teardown registry; drained by :func:`resetAfterEach`. */
export const cleanups: Array<() => void> = [];

/** The shared afterEach: drop fakes, the env override, and the module chain. */
export async function resetAfterEach(): Promise<void> {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup();
  clearSpecfloBin();
  // The anchor is module state and the module is cached, so each case must
  // forget the chain the previous one built.
  const { resetChainForTests } = await import("../src/index.ts");
  resetChainForTests();
}

/** Cold-start a loaded extension whose fake specflo replays ``baseline``. */
export async function coldStart(baseline: string) {
  const fake = createFakeSpecflo(baseline);
  cleanups.push(() => fake.cleanup());
  const factory = await loadExtension(fake.bin);
  const pi = createFakePi();
  factory(pi.api);
  await pi.emit({ type: "session_start", reason: "startup" }, createFakeCtx({ cwd: fake.root }));
  return { fake, pi };
}

/**
 * Fire one armed turn_end whose status poll returns ``snapshot``; usage is 80%
 * against the 75 threshold, so the turn always arms.
 */
export async function armedTurn(
  pi: Awaited<ReturnType<typeof coldStart>>["pi"],
  fake: Awaited<ReturnType<typeof coldStart>>["fake"],
  snapshot: string,
) {
  fake.setStdout(snapshot);
  const ctx = createFakeCtx({
    cwd: fake.root,
    contextUsage: { tokens: 160000, contextWindow: 200000, percent: 80 },
  });
  await pi.emit({ type: "turn_end", turnIndex: 0, message: {}, toolResults: [] }, ctx);
  return ctx;
}

/** The handler pi registered under `specflo-continue`. */
export function continueHandler(pi: Awaited<ReturnType<typeof coldStart>>["pi"]) {
  const command = pi.commands.get("specflo-continue");
  assert.ok(command, "the extension must register /specflo-continue");
  return command.handler as (args: string, ctx: any) => Promise<void>;
}

/**
 * A fake ReplacedSessionContext: everything a clear's withSession receives and
 * a later fire uses - ui, cwd, waitForIdle, newSession, sendMessage. The test
 * controls when waitForIdle releases, so it can swap the fake specflo's stdout
 * between the seam poll and the fire's pass report.
 */
export function createFakeReplacement(cwd: string, options: { idleRejects?: boolean } = {}) {
  let releaseIdle: () => void = () => {};
  const idle = options.idleRejects
    ? Promise.reject(new Error("stale context"))
    : new Promise<void>((resolve) => (releaseIdle = resolve));
  if (options.idleRejects) idle.catch(() => {}); // observed via waitForIdle only
  const delivered: Array<{ message: any; options: any }> = [];
  const ctx = createFakeCtx({ cwd }) as FakeCtx & {
    waitForIdle(): Promise<void>;
    sendMessage(message: any, opts: any): Promise<void>;
  };
  ctx.waitForIdle = () => idle;
  ctx.sendMessage = async (message: any, opts: any) => {
    delivered.push({ message, options: opts });
  };
  return { ctx, releaseIdle, delivered };
}

/** Poll until ``predicate`` holds or ~2s pass. */
export async function until(predicate: () => boolean): Promise<void> {
  for (let i = 0; i < 200 && !predicate(); i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(predicate(), "condition never became true");
}

/**
 * Anchor the chain the way a real clear does: run the auto handler against a
 * recording ctx, then run the recorded withSession against a fake replacement
 * context - which the extension stashes as the live anchor.
 */
export async function anchorChain(pi: Awaited<ReturnType<typeof coldStart>>["pi"], fake: any) {
  fake.setStdout(autoReport());
  const commandCtx = createFakeCtx({ cwd: fake.root });
  await continueHandler(pi)("auto", commandCtx);
  assert.equal(commandCtx.newSessionCalls.length, 1, "the anchoring clear must open a session");
  const replacement = createFakeReplacement(fake.root);
  await (commandCtx.newSessionCalls[0] as any).withSession(replacement.ctx);
  return { commandCtx, replacement };
}

/** The ui calls ``ctx`` received for one method. */
export function calls(ctx: FakeCtx, method: string) {
  return ctx.ui.calls.filter((call) => call.method === method);
}
