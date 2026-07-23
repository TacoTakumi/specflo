/**
 * Seam detection: while armed, each turn_end polls specflo status --json and
 * declares a seam - a safe point to clear - when the phase differs from the last
 * observed snapshot, or a task has moved to done (T-11, REQ-07 / REQ-08).
 *
 * A task merely moving to in_progress is not a seam: clearing there would
 * discard an in-flight task, which REQ-08 forbids. The seam signature the
 * extension keeps is therefore the phase and the done count alone - the two
 * fields that move only at a safe point - so a snapshot whose sole change is a
 * task starting compares equal and declares nothing.
 *
 * The seam decision is a pure function over two snapshots, tested here directly;
 * the no-clear invariant (REQ-08) is tested through the handler against the fake
 * ctx, by counting newSession calls. Delivery - the attended notice (T-13) and
 * the unattended fire (T-16) - is layered on top of the declaration, so seam
 * detection itself still initiates no clear; the handler tests pin that
 * boundary.
 */

import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { isSeam, parseSnapshot } from "../src/index.ts";
import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  loadExtension,
} from "./fake-ctx.ts";

/**
 * A status --json snapshot shaped like the real one, carrying the arming
 * threshold plus a phase, a done count, and how many tasks are in_progress.
 */
function status(opts: { phase?: string; done?: number; inProgress?: number } = {}): string {
  const phase = opts.phase ?? "execute";
  const done = opts.done ?? 11;
  const inProgress = opts.inProgress ?? 0;
  const total = 15;
  return JSON.stringify({
    phase,
    context_threshold_percent: 75,
    progress: {
      total,
      by_state: {
        pending: total - done - inProgress,
        in_progress: inProgress,
        done,
        blocked: 0,
      },
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
 * Fire one armed turn_end whose status poll returns ``snapshot`` and hand back
 * the ctx it ran against, for its newSession count. Usage is well above the
 * threshold so the turn always arms.
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
  await pi.emit({ type: "turn_end", turnIndex: 0, message: {}, toolResults: [] }, ctx);
  return ctx;
}

describe("seam detection", () => {
  it("declares a seam when the phase differs from the last snapshot", () => {
    // REQ-07: a phase change is a seam.
    const last = parseSnapshot(status({ phase: "execute" }))!;
    const now = parseSnapshot(status({ phase: "complete" }))!;
    assert.equal(isSeam(last, now), true);
  });

  it("declares no seam for a snapshot identical to the previous", () => {
    // REQ-07: an unchanged snapshot is not a seam.
    const snapshot = parseSnapshot(status({}))!;
    assert.equal(isSeam(snapshot, snapshot), false);
  });

  it("declares no seam when a task only moves to in_progress", () => {
    // REQ-07 / REQ-08: a task starting is work in flight, not a safe point. The
    // done count is unchanged, so the seam signature compares equal.
    const before = parseSnapshot(status({ done: 11, inProgress: 0 }))!;
    const after = parseSnapshot(status({ done: 11, inProgress: 1 }))!;
    assert.equal(isSeam(before, after), false);
  });

  it("declares a seam when a task moves to done", () => {
    // REQ-07: a task reaching done is a safe point.
    const before = parseSnapshot(status({ done: 11 }))!;
    const after = parseSnapshot(status({ done: 12 }))!;
    assert.equal(isSeam(before, after), true);
  });

  it("parses no snapshot from a failed or unparseable status", () => {
    assert.equal(parseSnapshot(null), null);
    assert.equal(parseSnapshot("not json"), null);
    assert.equal(parseSnapshot(""), null);
  });

  it("initiates no clear while armed when the only change is a task moving to in_progress", async () => {
    // REQ-08: armed, an unchanged-except-in_progress snapshot must not clear.
    const { fake, pi } = await coldStart(status({ done: 11, inProgress: 0 }));
    const ctx = await armedTurn(pi, fake, status({ done: 11, inProgress: 1 }));
    assert.equal(ctx.newSessionCalls.length, 0, "an in-flight task is never discarded");
  });

  it("initiates no clear while armed when the status poll returns nothing", async () => {
    // A poll that yields no parseable snapshot declares no seam and clears nothing.
    const { fake, pi } = await coldStart(status({}));
    const ctx = await armedTurn(pi, fake, "");
    assert.equal(ctx.newSessionCalls.length, 0);
  });

  it("declares a seam but initiates no clear in this slice", async () => {
    // Seam detection declares; the clear is T-13's (notice) and T-16's (fire)
    // work. An armed phase-change seam therefore acts on nothing yet.
    const { fake, pi } = await coldStart(status({ phase: "execute" }));
    const ctx = await armedTurn(pi, fake, status({ phase: "complete" }));
    assert.equal(ctx.newSessionCalls.length, 0);
  });
});
