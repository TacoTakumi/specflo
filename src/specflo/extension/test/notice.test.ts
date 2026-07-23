/**
 * The attended notice: an armed seam outside an auto run emits exactly one
 * passive ctx.ui.notify naming the current context usage, the seam that fired,
 * and the /specflo-continue command - and delivers nothing else (T-13,
 * REQ-09 / REQ-10).
 *
 * Passive means passive: no confirm/select/input dialog ever opens, no session
 * is cleared, and the notice text reaches ctx.ui alone - never model context.
 * The notice is per seam, not per turn: the baseline snapshot advances with
 * every parsed poll, so a second armed turn over an unchanged snapshot
 * declares no seam and says nothing (REQ-10).
 *
 * While an auto run is under way the seam is not the notice's to deliver -
 * that is the unattended fire (T-14) - so status reporting under_way true
 * emits nothing here.
 */

import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  type FakeCtx,
  loadExtension,
} from "./fake-ctx.ts";

/**
 * A status --json snapshot shaped like the real one: the arming threshold, the
 * auto-run block, and the phase and done count seam detection compares.
 */
function status(opts: { phase?: string; done?: number; autoUnderWay?: boolean } = {}): string {
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

const cleanups: Array<() => void> = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup();
  clearSpecfloBin();
});

/** Cold-start a loaded extension whose fake specflo replays ``baseline``. */
async function coldStart(baseline: string) {
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
 * against the 75 threshold, so the turn always arms. Returns the ctx (for its
 * ui and newSession counts) and the handler's result (which must stay
 * undefined - a turn_end injects nothing into model context).
 */
async function armedTurn(
  pi: Awaited<ReturnType<typeof coldStart>>["pi"],
  fake: Awaited<ReturnType<typeof coldStart>>["fake"],
  snapshot: string,
) {
  fake.setStdout(snapshot);
  const ctx = createFakeCtx({
    cwd: fake.root,
    contextUsage: { tokens: 160000, contextWindow: 200000, percent: 80 },
  });
  const result = await pi.emit({ type: "turn_end", turnIndex: 0, message: {}, toolResults: [] }, ctx);
  return { ctx, result };
}

/** The ui calls ``ctx`` received for one method. */
function calls(ctx: FakeCtx, method: string) {
  return ctx.ui.calls.filter((call) => call.method === method);
}

describe("attended notice", () => {
  it("emits one notify at an armed seam, naming the usage, the seam and the command", async () => {
    // REQ-09: one passive notice, and its three required ingredients.
    const { fake, pi } = await coldStart(status({ phase: "execute" }));

    const { ctx, result } = await armedTurn(pi, fake, status({ phase: "complete" }));

    const notifies = calls(ctx, "notify");
    assert.equal(notifies.length, 1, "an armed seam emits exactly one notify");
    const message = notifies[0].args[0] as string;
    assert.match(message, /80%/, "the notice names the current context usage");
    assert.match(message, /complete/, "the notice names the seam that fired");
    assert.match(message, /\/specflo-continue/, "the notice names the exact command to run");
    assert.equal(result, undefined, "a turn_end injects nothing into model context");
  });

  it("opens no dialog and clears nothing at an armed seam", async () => {
    // REQ-09: zero confirm/select/input calls and zero newSession calls.
    const { fake, pi } = await coldStart(status({ phase: "execute" }));

    const { ctx } = await armedTurn(pi, fake, status({ phase: "complete" }));

    assert.equal(calls(ctx, "confirm").length, 0);
    assert.equal(calls(ctx, "select").length, 0);
    assert.equal(calls(ctx, "input").length, 0);
    assert.equal(ctx.newSessionCalls.length, 0, "the attended notice never clears");
  });

  it("emits one notify in total across two armed turns with an unchanged snapshot", async () => {
    // REQ-10: once per seam, not once per turn. The first armed turn sees a
    // task reach done and notifies; the second sees the same snapshot and
    // declares nothing.
    const { fake, pi } = await coldStart(status({ done: 11 }));

    const first = await armedTurn(pi, fake, status({ done: 12 }));
    const second = await armedTurn(pi, fake, status({ done: 12 }));

    const total = calls(first.ctx, "notify").length + calls(second.ctx, "notify").length;
    assert.equal(total, 1, "an unchanged snapshot must not re-notify");
  });

  it("emits nothing at an armed turn that declares no seam", async () => {
    const { fake, pi } = await coldStart(status({}));

    const { ctx } = await armedTurn(pi, fake, status({}));

    assert.equal(ctx.ui.calls.length, 0, "no seam, no ui traffic at all");
  });

  it("hands an auto-run seam to the fire: unanchored, the bootstrap notice, not this one", async () => {
    // The seam is real, but its delivery belongs to the unattended fire
    // (T-16). Unanchored - as any cold-started process is - the fire degrades
    // to the bootstrap notice naming /specflo-continue auto (REQ-31), and
    // nothing clears.
    const { fake, pi } = await coldStart(status({ phase: "execute" }));

    const { ctx } = await armedTurn(pi, fake, status({ phase: "complete", autoUnderWay: true }));

    const notices = calls(ctx, "notify");
    assert.equal(notices.length, 1, "an unanchored auto seam emits the bootstrap notice");
    assert.match(notices[0].args[0] as string, /\/specflo-continue auto/);
    assert.equal(ctx.newSessionCalls.length, 0, "and this slice still clears nothing");
  });
});
