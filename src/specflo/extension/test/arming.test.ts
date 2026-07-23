/**
 * Arming: at each turn_end the extension arms on context-usage percent against
 * the threshold status --json reports, and treats unknown usage as unarmed
 * (T-10, REQ-04 / REQ-05 / REQ-26 / REQ-28).
 *
 * The arming check itself is the in-process getContextUsage() read alone - no
 * subprocess (REQ-26). Its only visible effect in this slice is the armed seam
 * poll: while armed a turn_end runs status --json, while unarmed it runs
 * nothing. So "did the extension arm?" is read here as "did the turn_end spawn
 * the poll?" - which is exactly the process count REQ-26 pins.
 *
 * The threshold is seeded from the cold-start status snapshot, so every case
 * emits a startup session_start first; the poll count is measured as the delta
 * a single turn_end adds on top of that seeding.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { afterEach, describe, it } from "node:test";

import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  type FakeCtxOptions,
  loadExtension,
} from "./fake-ctx.ts";

/** A status --json snapshot reporting ``percent`` as the arming threshold. */
const statusWithThreshold = (percent: number): string =>
  JSON.stringify({ context_threshold_percent: percent });

const cleanups: Array<() => void> = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup();
  clearSpecfloBin();
});

/**
 * A loaded extension whose fake specflo reports ``statusJson``, with the
 * cold-start threshold already seeded by a startup session_start.
 */
async function setUp(statusJson: string) {
  const fake = createFakeSpecflo(statusJson);
  cleanups.push(() => fake.cleanup());
  const factory = await loadExtension(fake.bin);
  const pi = createFakePi();
  factory(pi.api);
  await pi.emit({ type: "session_start", reason: "startup" }, createFakeCtx({ cwd: fake.root }));
  return { fake, pi };
}

/**
 * Fire one turn_end reporting ``contextUsage`` and report the arming outcome:
 * how many subprocesses the turn spawned (the seam poll fires iff armed) and
 * the ctx it ran against (for its newSession count).
 */
async function turnEnd(
  pi: Awaited<ReturnType<typeof setUp>>["pi"],
  fake: Awaited<ReturnType<typeof setUp>>["fake"],
  contextUsage: FakeCtxOptions["contextUsage"],
) {
  const before = fake.invocations().length;
  const ctx = createFakeCtx({ cwd: fake.root, contextUsage });
  await pi.emit({ type: "turn_end", turnIndex: 0, message: {}, toolResults: [] }, ctx);
  return { polls: fake.invocations().length - before, ctx };
}

describe("arming", () => {
  it("arms above the reported threshold and not below", async () => {
    // REQ-04: threshold 75, a 74% turn stays unarmed, a 76% turn arms.
    const { fake, pi } = await setUp(statusWithThreshold(75));

    const below = await turnEnd(pi, fake, { tokens: 148000, contextWindow: 200000, percent: 74 });
    assert.equal(below.polls, 0, "a turn below the threshold must not arm, so it must not poll");

    const above = await turnEnd(pi, fake, { tokens: 152000, contextWindow: 200000, percent: 76 });
    assert.equal(above.polls, 1, "a turn above the threshold arms, and an armed turn takes the seam poll");
  });

  it("spawns no subprocess on the arming check below the threshold", async () => {
    // REQ-26: the per-turn arming check is the getContextUsage() read alone.
    const { fake, pi } = await setUp(statusWithThreshold(75));

    const { polls } = await turnEnd(pi, fake, { tokens: 100000, contextWindow: 200000, percent: 50 });

    assert.equal(polls, 0);
  });

  it("treats null tokens and null percent as unarmed", async () => {
    // REQ-05: the shape getContextUsage() returns right after compaction.
    const { fake, pi } = await setUp(statusWithThreshold(75));

    const { polls, ctx } = await turnEnd(pi, fake, {
      tokens: null,
      contextWindow: 200000,
      percent: null,
    });

    assert.equal(polls, 0, "null usage must not arm");
    assert.equal(ctx.newSessionCalls.length, 0, "unknown usage initiates no clear");
  });

  it("treats undefined usage as unarmed", async () => {
    // REQ-05: getContextUsage() returns undefined before the first reading.
    const { fake, pi } = await setUp(statusWithThreshold(75));

    const { polls, ctx } = await turnEnd(pi, fake, undefined);

    assert.equal(polls, 0, "undefined usage must not arm");
    assert.equal(ctx.newSessionCalls.length, 0, "unknown usage initiates no clear");
  });

  it("reads context usage as a percent, never a token count", async () => {
    // REQ-04: no absolute token constant in the arming path. The extension
    // compares getContextUsage().percent and never reads .tokens at all, so a
    // token count can never enter the arming decision.
    const source = fs.readFileSync(path.join(import.meta.dirname, "..", "src", "index.ts"), "utf8");

    assert.ok(/\.percent\b/.test(source), "arming must read the usage percent");
    assert.ok(!/\.tokens\b/.test(source), "the extension must never read the raw token count");
  });
});
