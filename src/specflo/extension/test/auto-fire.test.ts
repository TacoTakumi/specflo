/**
 * The unattended fire: an anchored armed seam while an auto run is under way
 * clears the session and reseeds the `specflo auto` payload with no user
 * input; an unanchored one emits the bootstrap notice instead; and the loop
 * stops dead when the CLI's pass report says stop (T-16, REQ-29 / REQ-30 /
 * REQ-31 / REQ-13).
 *
 * The fire runs through the live anchor: pi gives event contexts no
 * newSession and dispatches no extension-queued slash command, so the armed
 * turn_end clears through the ReplacedSessionContext the previous clear
 * captured (REQ-29), detached behind waitForIdle. Loop control never leaves
 * the CLI: the fire runs `specflo auto --json` once, reads its stop verdict,
 * and holds no pass counter and no cap of its own (REQ-13).
 *
 * The chain also proves the loop can run more than once per process: pi
 * builds a fresh extension closure for every session, so the `new` session a
 * fire opens must re-seed the arming state - without re-injecting the
 * cold-start payload the fire already delivered.
 *
 * The rig - snapshots, drivers, and the fake anchor chain - lives in
 * auto-helpers.ts, shared with the abort coverage (abort.test.ts).
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { afterEach, describe, it } from "node:test";

import {
  AUTO_PAYLOAD,
  anchorChain,
  armedTurn,
  autoReport,
  calls,
  cleanups,
  coldStart,
  continueHandler,
  createFakeReplacement,
  resetAfterEach,
  status,
  until,
} from "./auto-helpers.ts";
import { createFakeCtx, createFakePi, createFakeSpecflo, loadExtension } from "./fake-ctx.ts";

afterEach(resetAfterEach);

describe("the unattended fire", () => {
  it("notices /specflo-continue auto at an unanchored armed auto seam, and clears nothing", async () => {
    // REQ-31: no anchor - pi joined the run cold - so the seam degrades to one
    // bootstrap notice naming the command that anchors the chain.
    const { fake, pi } = await coldStart(status({ phase: "execute", autoUnderWay: true }));

    const ctx = await armedTurn(pi, fake, status({ phase: "complete", autoUnderWay: true }));

    const notices = calls(ctx, "notify");
    assert.equal(notices.length, 1, "an unanchored auto seam emits exactly one notice");
    assert.match(notices[0].args[0] as string, /\/specflo-continue auto/);
    assert.equal(ctx.newSessionCalls.length, 0, "unanchored means nothing can clear");
    assert.equal(pi.sent.length, 0, "nothing is ever queued into model context");
  });

  it("fires the anchored seam: one newSession, the auto payload verbatim, zero dialogs", async () => {
    // REQ-31 anchored + REQ-13: the fire waits for idle, reads one pass
    // report, and delivers its payload into the session it opens.
    const { fake, pi } = await coldStart(status({ phase: "execute", autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);

    const turnCtx = await armedTurn(pi, fake, status({ phase: "complete", autoUnderWay: true }));
    // The fire is holding at waitForIdle; the pass report replaces the seam
    // snapshot only now, exactly as the real CLI would answer the later call.
    const before = fake.invocations().length;
    fake.setStdout(autoReport());
    replacement.releaseIdle();
    await until(() => replacement.ctx.newSessionCalls.length === 1);

    assert.deepEqual(
      fake.invocations().slice(before),
      ["auto --json"],
      "one pass report between the seam and the clear, and nothing else",
    );
    const second = createFakeReplacement(fake.root);
    await (replacement.ctx.newSessionCalls[0] as any).withSession(second.ctx);
    assert.equal(second.delivered.length, 1, "one message into the new session");
    assert.equal(
      Buffer.compare(Buffer.from(second.delivered[0].message.content), Buffer.from(AUTO_PAYLOAD)),
      0,
      "the delivered message must be the CLI's payload byte for byte",
    );
    assert.equal(second.delivered[0].message.display, false);
    assert.equal(second.delivered[0].options.triggerTurn, true, "the reseed must run a turn");
    for (const ctx of [turnCtx, replacement.ctx]) {
      assert.equal(calls(ctx, "confirm").length, 0);
      assert.equal(calls(ctx, "select").length, 0);
      assert.equal(calls(ctx, "input").length, 0);
    }
    assert.equal(calls(turnCtx, "notify").length, 0, "an anchored fire says nothing");
    assert.equal(turnCtx.newSessionCalls.length, 0, "the event context itself never clears");
  });

  it("fires at most one clear while a fire is already in flight", async () => {
    // Two seams before the first clear lands must not open two sessions.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    const { replacement } = await anchorChain(pi, fake);

    await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));
    await armedTurn(pi, fake, status({ done: 13, autoUnderWay: true }));
    fake.setStdout(autoReport());
    replacement.releaseIdle();
    await until(() => replacement.ctx.newSessionCalls.length === 1);
    // Settle any second fire that would be racing; the count must hold at one.
    await new Promise((resolve) => setTimeout(resolve, 50));

    assert.equal(replacement.ctx.newSessionCalls.length, 1);
  });

  it("drops a stale anchor and notices at the next armed auto seam", async () => {
    // REQ-31: the anchor goes stale when something other than the chain
    // replaces the session; the fire fails, and the chain degrades to the
    // bootstrap notice instead of erroring forever.
    const { fake, pi } = await coldStart(status({ done: 11, autoUnderWay: true }));
    fake.setStdout(autoReport());
    const commandCtx = createFakeCtx({ cwd: fake.root });
    await continueHandler(pi)("auto", commandCtx);
    const stale = createFakeReplacement(fake.root, { idleRejects: true });
    await (commandCtx.newSessionCalls[0] as any).withSession(stale.ctx);

    const first = await armedTurn(pi, fake, status({ done: 12, autoUnderWay: true }));
    // Let the detached fire hit the rejection and drop the anchor.
    await new Promise((resolve) => setTimeout(resolve, 20));
    const second = await armedTurn(pi, fake, status({ done: 13, autoUnderWay: true }));

    assert.equal(calls(first, "notify").length, 0, "the anchored seam itself says nothing");
    assert.equal(stale.ctx.newSessionCalls.length, 0, "a stale anchor must not clear");
    const notices = calls(second, "notify");
    assert.equal(notices.length, 1, "the next seam degrades to the bootstrap notice");
    assert.match(notices[0].args[0] as string, /\/specflo-continue auto/);
  });

  it("clears once and delivers the auto payload verbatim on a continuable handler pass", async () => {
    // The user-typed `/specflo-continue auto`: the anchoring opt-in.
    const { fake, pi } = await coldStart(status({ autoUnderWay: true }));
    fake.setStdout(autoReport());
    const ctx = createFakeCtx({ cwd: fake.root });

    await continueHandler(pi)("auto", ctx);

    assert.equal(ctx.newSessionCalls.length, 1, "a continuable pass clears exactly once");
    const sinceColdStart = fake.invocations().slice(2);
    assert.deepEqual(sinceColdStart, ["auto --json"], "one pass report, and nothing else");
    const replacement = createFakeReplacement(fake.root);
    await (ctx.newSessionCalls[0] as any).withSession(replacement.ctx);
    assert.equal(replacement.delivered.length, 1, "one message into the new session");
    assert.equal(
      Buffer.compare(
        Buffer.from(replacement.delivered[0].message.content),
        Buffer.from(AUTO_PAYLOAD),
      ),
      0,
      "the delivered message must be the CLI's payload byte for byte",
    );
    assert.equal(calls(ctx, "confirm").length, 0);
    assert.equal(calls(ctx, "select").length, 0);
    assert.equal(calls(ctx, "input").length, 0);
  });

  for (const reason of ["kill-switch", "pass-cap", "stall", "project-complete"]) {
    it(`clears nothing when the CLI reports stop on ${reason}`, async () => {
      // REQ-13: the CLI's verdict is final - no clear, and no second report
      // that would start another pass. The CLI's stop directive reaches the
      // user through ctx.ui alone, never model context.
      const directive = `== specflo auto halted ==\nStopped: ${reason}.`;
      const { fake, pi } = await coldStart(status({ autoUnderWay: true }));
      fake.setStdout(autoReport({ payload: directive, stop: true, reason }));
      const ctx = createFakeCtx({ cwd: fake.root });

      await continueHandler(pi)("auto", ctx);

      assert.equal(ctx.newSessionCalls.length, 0, "a stop verdict must not clear");
      const sinceColdStart = fake.invocations().slice(2);
      assert.deepEqual(sinceColdStart, ["auto --json"], "one report, no further pass");
      const notices = calls(ctx, "notify");
      assert.equal(notices.length, 1, "the stop directive must reach the user");
      assert.equal(notices[0].args[0], directive, "as the CLI's own words, verbatim");
    });
  }

  it("clears nothing and says nothing on an empty or unparseable pass report", async () => {
    // STOP_UNAVAILABLE carries an empty payload; a malformed report reads the
    // same way. Neither has anything to deliver - to the session or the user.
    const { fake, pi } = await coldStart(status({ autoUnderWay: true }));
    for (const stdout of [autoReport({ payload: "", stop: true, reason: "unavailable" }), "not json"]) {
      fake.setStdout(stdout);
      const ctx = createFakeCtx({ cwd: fake.root });

      await continueHandler(pi)("auto", ctx);

      assert.equal(ctx.newSessionCalls.length, 0);
      assert.equal(ctx.ui.calls.length, 0);
    }
  });

  it("seeds arming state on a new session without re-injecting the cold-start payload", async () => {
    // pi rebuilds the extension closure per session, so the `new` session a
    // fire opens starts blank. Its session_start must re-seed the threshold
    // and the seam baseline - or the loop arms at most once per process -
    // while the cold-start injection stays where it belongs: the fire already
    // delivered its payload.
    const fake = createFakeSpecflo(status({ phase: "execute", autoUnderWay: true }));
    cleanups.push(() => fake.cleanup());
    const factory = await loadExtension(fake.bin);
    const pi = createFakePi();
    factory(pi.api);

    await pi.emit({ type: "session_start", reason: "new" }, createFakeCtx({ cwd: fake.root }));

    assert.deepEqual(fake.invocations(), ["status --json"], "seed arming state, fetch no payload");
    const injected = await pi.emit({ type: "before_agent_start" }, createFakeCtx({ cwd: fake.root }));
    assert.equal(injected, undefined, "a new session injects no cold-start payload");

    // The re-seeded state must actually re-arm: the next armed auto seam is
    // seen (here unanchored, so the visible effect is the bootstrap notice).
    const ctx = await armedTurn(pi, fake, status({ phase: "complete", autoUnderWay: true }));
    assert.equal(calls(ctx, "notify").length, 1, "the continued session must re-arm");
  });

  it("holds no pass counter and no cap constant, and reads the CLI's stop verdict", async () => {
    // REQ-13 acceptance: loop control lives in the CLI. The source reads the
    // pass report's stop field and never counts or caps anything itself.
    const source = fs.readFileSync(path.join(import.meta.dirname, "..", "src", "index.ts"), "utf8");

    assert.ok(/\bstop\b/.test(source), "the extension must read the CLI's stop verdict");
    assert.ok(
      !/passes|passCount|pass_count|maxPass|max_pass|MAX_PASS/.test(source),
      "the extension must hold no pass counter and no cap constant",
    );
  });
});
