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
    //
    // This is also the third state pi-fixes-2 REQ-06 holds unchanged, and the
    // one the dispatch fix must not have swallowed: the latch still exists and
    // still latches, for a fire that is *genuinely* in flight. What moved is
    // only its release point - here the fire is parked at waitForIdle, which
    // the test controls, so it is held for real and seam two must record
    // nothing.
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

    // The fire's own clear re-anchored the chain on the way through - the fake
    // ran withSession against the replacement it built, as pi does - so the
    // latch is released and a later seam ends the next run too.
    const next = replacement.ctx.replacements[0];
    assert.ok(next, "the fire's clear must have handed withSession a replacement");
    const later = await armedTurn(pi, fake, status({ done: 14, autoUnderWay: true }));

    assert.equal(later.abortCalls.length, 1, "a post-release seam aborts again");
    fake.setStdout(autoReport());
    next.releaseIdle();
    await until(() => next.ctx.newSessionCalls.length === 1);
  });

  it("releases the latch at delivery, not at the end of the turn the reseed starts", async () => {
    // REQ-02 / REQ-05: the fire holds fireInFlight only until the replacement
    // is anchored and the payload handed over. A replacement whose sendMessage
    // records its message and then never settles stands in for the child turn
    // that reseed starts - which on a real pi runs for minutes. If the fire
    // waits on it, the latch is held for that whole run and every later seam
    // is swallowed: the first-fire-only bug.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);
    // The anchoring clear's own send has already settled; the flag is read at
    // send time, so from here every reseed the chain delivers stays pending.
    replacement.options.sendNeverSettles = true;

    const first = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));
    assert.equal(first.abortCalls.length, 1, "the first seam aborts the run");

    fake.setStdout(autoReport());
    replacement.releaseIdle();
    await until(() => replacement.ctx.newSessionCalls.length === 1);

    // The fire opened the replacement and delivered into it; its send is still
    // pending, so everything below runs while the child turn is "in progress".
    const next = replacement.ctx.replacements[0];
    assert.ok(next, "the fire's clear must have handed withSession a replacement");
    assert.equal(next.delivered.length, 1, "the payload reached the new session");

    // The claim: the continuation completed, the latch released, and the next
    // armed anchored seam fires through the anchor this fire installed.
    const second = await armedTurn(pi, fake, status({ done: 13, autoUnderWay: true }));
    assert.equal(
      second.abortCalls.length,
      1,
      "the latch must release at delivery: the next seam aborts",
    );
    fake.setStdout(autoReport());
    next.releaseIdle();
    await until(() => next.ctx.newSessionCalls.length === 1);
    assert.equal(next.ctx.newSessionCalls.length, 1, "and fires a second continuation");
  });

  it("degrades to the bootstrap notice when the reseed dispatch fails", async () => {
    // REQ-04: dispatching rather than awaiting means a failed send is nobody's
    // return value, so it has to be caught where it is thrown. A session that
    // received nothing is no anchor: the .catch drops it, and the next armed
    // auto seam falls back to the branch pi joining a run cold already takes -
    // one notice naming the command that re-anchors, nothing ended, nothing
    // cleared. The rejection must also stay inside the extension: an escaped
    // one takes pi's whole process down.
    const rejections: unknown[] = [];
    const onRejection = (reason: unknown) => rejections.push(reason);
    process.on("unhandledRejection", onRejection);
    try {
      const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
      const { replacement } = await anchorChain(pi, fake);
      // The anchoring clear's own send already landed; from here every reseed
      // the chain dispatches rejects.
      replacement.options.sendRejects = true;

      await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));
      fake.setStdout(autoReport());
      replacement.releaseIdle();
      await until(() => replacement.ctx.newSessionCalls.length === 1);
      // Let the rejection reach the .catch and drop the anchor.
      await new Promise((resolve) => setTimeout(resolve, 50));

      const later = await armedTurn(pi, fake, status({ done: 13, autoUnderWay: true }));

      assert.equal(later.abortCalls.length, 0, "a dropped anchor must not end the run");
      assert.equal(later.newSessionCalls.length, 0, "and must not clear");
      const notices = calls(later, "notify");
      assert.equal(notices.length, 1, "exactly one bootstrap notice");
      assert.match(notices[0].args[0] as string, /\/specflo-continue auto/);

      // Node reports an unhandled rejection a tick or two late; give it room.
      await new Promise((resolve) => setTimeout(resolve, 100));
      assert.deepEqual(rejections, [], "the failed dispatch must not escape the extension");
    } finally {
      process.off("unhandledRejection", onRejection);
    }
  });
});

// Two of the three states pi-fixes-2 REQ-06 holds unchanged (the third is the
// parked-fire case above). Neither touches the reseed, so neither should have
// moved when it started dispatching - which is exactly why they are re-run
// against the reshaped fake rather than taken on trust.
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

describe("the reseed is dispatched, not awaited", () => {
  // REQ-05 structural acceptance, alongside the scans above. The behavioural
  // half is the never-settling case at the top of this file; this one catches
  // the shape being quietly restored. Reinstating the await is a one-word edit
  // that turns the chain back into a single fire and breaks nothing else -
  // every unit case that does not park a fire keeps passing - so the shape is
  // worth pinning in the source directly.
  const source = fs.readFileSync(path.join(import.meta.dirname, "..", "src", "index.ts"), "utf8");

  /** reseedInto's body: its signature through the first column-zero brace. */
  const reseedBody = (): string => {
    const start = source.indexOf("async function reseedInto");
    assert.notEqual(start, -1, "reseedInto must exist in src/index.ts");
    const end = source.indexOf("\n}", start);
    assert.notEqual(end, -1, "reseedInto must close at column zero");
    // The doc comment sits above the signature and is deliberately excluded:
    // it discusses awaiting at length, and prose must not answer this.
    return source.slice(start, end + 2);
  };

  it("awaits the clear and nothing else - never the reseed send", () => {
    const body = reseedBody();
    // Whitespace-tolerant: the call is formatted across lines.
    assert.match(body, /session\s*\.\s*sendMessage\(/, "the scan must be reading the reseed send");
    const awaits = body.match(/\bawait\b/g) ?? [];
    assert.deepEqual(
      awaits,
      ["await"],
      `reseedInto must hold exactly one await, the clear's; found ${awaits.length}:\n${body}`,
    );
    assert.match(body, /await ctx\.newSession\(/, "and that one await is ctx.newSession");
  });

  it("hands the dispatched send its own catch", () => {
    // A dispatched promise nobody awaits takes pi's process down when it
    // rejects. The behaviour that catch buys is pinned above (REQ-04).
    assert.match(reseedBody(), /\.catch\(/, "the dispatched send must handle its own rejection");
  });
});
