/**
 * The abort at the armed auto anchored seam (T-03, REQ-01 / REQ-03 / REQ-06):
 * a turn_end in the armed + auto-under-way + anchored state with a declared
 * seam ends the running agent via ctx.abort() - once, whatever the seam kind -
 * while every other state aborts nothing. The abort composes with the
 * fireInFlight latch: a second seam during a parked fire neither double-aborts
 * nor double-fires, and the latch releases once the fire lands.
 *
 * Same rig as auto-fire.test.ts (auto-helpers.ts): the fake specflo replays
 * snapshots, the fake anchor chain stands in for the previous clear, and the
 * fake ctx records ctx.abort() calls.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { afterEach, describe, it } from "node:test";

import {
  anchorChain,
  armedTurn,
  autoReport,
  calls,
  createFakeReplacement,
  coldStart,
  resetAfterEach,
  status,
  until,
} from "./auto-helpers.ts";
import { createFakeCtx } from "./fake-ctx.ts";

afterEach(resetAfterEach);

describe("the abort at the armed auto anchored seam", () => {
  it("records exactly one abort at a phase-change seam", async () => {
    // REQ-01 + REQ-03: the seam ends the run; the seam kind does not matter.
    const { fake, pi } = await coldStart(status({ phase: "execute", autoUnderWay: true }));
    await anchorChain(pi, fake);

    const ctx = await armedTurn(pi, fake, status({ phase: "complete", autoUnderWay: true }));

    assert.equal(ctx.abortCalls.length, 1, "the anchored armed auto seam must abort the run");
  });

  it("records exactly one abort at a task-done seam", async () => {
    // REQ-03: the sail-on's own seam kind - a done count that went up.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    await anchorChain(pi, fake);

    const ctx = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));

    assert.equal(ctx.abortCalls.length, 1, "the anchored armed auto seam must abort the run");
  });

  it("records zero aborts when armed with no seam", async () => {
    // Anchored and armed, but the snapshot never moved: nothing to end.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);

    const ctx = await armedTurn(pi, fake, status({ done: 11, autoUnderWay: true }));

    assert.equal(ctx.abortCalls.length, 0);
    assert.equal(replacement.ctx.newSessionCalls.length, 0, "no seam, no fire");
  });

  it("records zero aborts when unarmed", async () => {
    // Anchored, a seam waiting in the snapshot - but usage is under the
    // threshold, so the handler returns at the arming check.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);

    fake.setStdout(status({ done: 12, autoUnderWay: true }));
    const ctx = createFakeCtx({
      cwd: fake.root,
      contextUsage: { tokens: 100000, contextWindow: 200000, percent: 50 },
    });
    await pi.emit({ type: "turn_end", turnIndex: 0, message: {}, toolResults: [] }, ctx);

    assert.equal(ctx.abortCalls.length, 0);
    assert.equal(replacement.ctx.newSessionCalls.length, 0, "unarmed, nothing fires");
  });

  it("records zero aborts at an unanchored auto seam", async () => {
    // REQ-04 keeps this branch notify-only; here only the abort is pinned -
    // the notice itself is T-05's guard.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));

    const ctx = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));

    assert.equal(ctx.abortCalls.length, 0, "unanchored means nothing may end the run");
  });

  it("records zero aborts at an attended armed seam", async () => {
    // Attended, even with a live anchor from an earlier auto pass: the seam
    // only notifies, and the run keeps going.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    await anchorChain(pi, fake);

    const ctx = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: false }));

    assert.equal(ctx.abortCalls.length, 0, "an attended seam must not end the run");
  });

  it("aborts once across two seams during a parked fire, then again after release", async () => {
    // REQ-06: the abort sits behind the fireInFlight latch. Seam one parks
    // the fire and aborts; seam two, declared while the fire still holds at
    // waitForIdle, does neither; and once the fire lands and re-anchors, the
    // next seam aborts and fires again.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);

    const first = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));
    const second = await armedTurn(pi, fake, status({ done: 13, autoUnderWay: true }));

    assert.equal(first.abortCalls.length, 1, "the first seam aborts the run");
    assert.equal(second.abortCalls.length, 0, "a parked fire latches the second seam out");

    // Land the first fire: one continuation, exactly as auto-fire pins it.
    fake.setStdout(autoReport());
    replacement.releaseIdle();
    await until(() => replacement.ctx.newSessionCalls.length === 1);
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.equal(replacement.ctx.newSessionCalls.length, 1, "one abort, one continuation");

    // The fire's clear re-anchors, as production's withSession does; the
    // latch is released, so a later seam ends the next run too.
    const next = createFakeReplacement(fake.root);
    await (replacement.ctx.newSessionCalls[0] as any).withSession(next.ctx);
    const later = await armedTurn(pi, fake, status({ done: 14, autoUnderWay: true }));

    assert.equal(later.abortCalls.length, 1, "a post-release seam aborts again");
    fake.setStdout(autoReport());
    next.releaseIdle();
    await until(() => next.ctx.newSessionCalls.length === 1);
  });
});

describe("the notify-only branches stay notify-only", () => {
  it("an unanchored armed auto seam: one notice, zero aborts, zero sessions (REQ-04)", async () => {
    // pi joined the run cold - nothing may end the run or clear the session;
    // the one bootstrap notice names the command that anchors the chain.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));

    const ctx = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));

    const notices = calls(ctx, "notify");
    assert.equal(notices.length, 1, "exactly one bootstrap notice");
    assert.match(notices[0].args[0] as string, /\/specflo-continue auto/);
    assert.equal(ctx.abortCalls.length, 0, "an unanchored seam must not end the run");
    assert.equal(ctx.newSessionCalls.length, 0, "an unanchored seam must not clear");
  });

  it("an attended armed seam: one notice, zero aborts, zero sessions (REQ-04)", async () => {
    // Attended even with a live anchor from an earlier auto pass: the seam
    // says so once, passively, and the run keeps going.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);

    const ctx = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: false }));

    const notices = calls(ctx, "notify");
    assert.equal(notices.length, 1, "exactly one attended notice");
    assert.doesNotMatch(
      notices[0].args[0] as string,
      /\/specflo-continue auto/,
      "the attended notice names the attended command, not the auto opt-in",
    );
    assert.equal(ctx.abortCalls.length, 0, "an attended seam must not end the run");
    assert.equal(ctx.newSessionCalls.length, 0, "an attended seam must not clear");
    assert.equal(replacement.ctx.newSessionCalls.length, 0, "the anchor must not fire attended");
  });
});

describe("the armed path stays phase-blind and injection-free", () => {
  // REQ-03 / REQ-05 structural acceptance, in the style of auto-fire's
  // no-pass-counter scan: read the source and pin what must not appear.
  const source = fs.readFileSync(path.join(import.meta.dirname, "..", "src", "index.ts"), "utf8");

  it("the armed turn_end path holds no phase conditional", () => {
    // Everything from the turn_end registration on is the armed path; seam
    // detection (describeSeam, defined above it) is where phases may appear.
    const armedPath = source.slice(source.indexOf('pi.on("turn_end"'));
    assert.ok(armedPath.length > 0, "the turn_end registration must exist");
    assert.ok(
      !/phase/i.test(armedPath),
      "the armed turn_end path must not mention phases at all (D-03)",
    );
  });

  it("the extension injects nothing: no sendCustomMessage, no steer, no tool_result edits", () => {
    assert.ok(
      !/sendCustomMessage/.test(source),
      "the extension must never inject into the current session's context",
    );
    assert.ok(!/steer/i.test(source), "the extension must never steer a running turn");
    assert.ok(
      !/tool_?results?/i.test(source),
      "the extension must never touch tool results",
    );
  });
});
