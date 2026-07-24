/**
 * The /specflo-continue command: clear the session and reseed the active
 * project's continuation payload on demand (T-12, REQ-11 / REQ-14 / REQ-22 /
 * REQ-27).
 *
 * These drive the registered command handler directly against a fake pi and a
 * fake `specflo`, so each case pins one thing a live run cannot be steered into
 * on demand: an active project (one clear, the direct-continuation payload
 * delivered verbatim into the new session), and no active project (nothing
 * cleared, a notice saying why). The end-to-end suite proves the same inside a
 * real pi.
 */

import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { replacementFactory } from "./auto-helpers.ts";
import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  loadExtension,
  type FakePi,
} from "./fake-ctx.ts";

/** A continuation payload with bytes an edit or a re-template would disturb. */
const PAYLOAD =
  "== specflo next step ==\nCarry out the next step now.\n" +
  "# Checkpoint - demo\nline with trailing spaces   \n\tand a tab\n";

const cleanups: Array<() => void> = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup();
  clearSpecfloBin();
});

/** A loaded extension wired to a fake specflo that replays ``stdout``. */
async function setUp(stdout: string) {
  const fake = createFakeSpecflo(stdout);
  cleanups.push(() => fake.cleanup());
  const factory = await loadExtension(fake.bin);
  const pi = createFakePi();
  factory(pi.api);
  return {
    fake,
    pi,
    ctx: createFakeCtx({ cwd: fake.root, replacement: replacementFactory() }),
  };
}

/** The handler pi registered under `specflo-continue`. */
function continueHandler(pi: FakePi): (args: string, ctx: any) => Promise<void> {
  const command = pi.commands.get("specflo-continue");
  assert.ok(command, "the extension must register /specflo-continue");
  return command.handler;
}

/**
 * Everything the handler delivered into the session it opened, in order.
 *
 * The fake ctx replaces the session the way pi does - it builds the
 * replacement and runs the handler's ``withSession`` against it - so this
 * reads back what the command actually put there, with nothing invoked by
 * hand afterwards (REQ-07). Both delivery doors are recorded, so a message
 * that went out as a user message would still be counted here.
 */
function deliveredInto(ctx: any): Array<{ message: any; options: any }> {
  const replacement = ctx.replacements[0];
  assert.ok(replacement, "the clear must have handed withSession a replacement session");
  return replacement.delivered;
}

describe("the /specflo-continue command", () => {
  it("is registered without registering a tool", async () => {
    const { pi } = await setUp(PAYLOAD);

    assert.ok(pi.commands.has("specflo-continue"));
    assert.deepEqual(pi.tools, []);
  });

  it("clears once and delivers the direct-continuation payload verbatim", async () => {
    const { fake, pi, ctx } = await setUp(PAYLOAD);

    await continueHandler(pi)("", ctx);

    // Exactly one clear...
    assert.equal(ctx.newSessionCalls.length, 1);
    // ...fed by `hook reseed --continue` and nothing else (REQ-22).
    assert.deepEqual(fake.invocations(), ["hook reseed --continue"]);

    const delivered = deliveredInto(ctx);
    assert.equal(delivered.length, 1, "one message into the new session");
    assert.equal(delivered[0].message.content, PAYLOAD);
    assert.equal(
      Buffer.compare(Buffer.from(delivered[0].message.content), Buffer.from(PAYLOAD)),
      0,
      "the delivered message must be the CLI's stdout byte for byte",
    );
    assert.equal(delivered[0].options.triggerTurn, true, "the reseed must run a turn");
  });

  it("adds no prose of its own to the delivered payload", async () => {
    // REQ-27: equal byte length means no prefix, wrapper or header was added.
    const { pi, ctx } = await setUp(PAYLOAD);

    await continueHandler(pi)("", ctx);
    const delivered = deliveredInto(ctx);

    assert.equal(delivered[0].message.content.length, PAYLOAD.length);
    assert.equal(delivered[0].message.display, false);
  });

  it("clears nothing and reports why when there is no active project", async () => {
    // `specflo hook reseed --continue` prints nothing with no active project.
    const { fake, pi, ctx } = await setUp("");

    await continueHandler(pi)("", ctx);

    assert.equal(ctx.newSessionCalls.length, 0, "nothing to continue, so nothing cleared");
    assert.deepEqual(fake.invocations(), ["hook reseed --continue"]);
    const notices = ctx.ui.calls.filter((call) => call.method === "notify");
    assert.equal(notices.length, 1, "a run with no project must say why");
  });

  it("reports why through ctx.ui only, never into model context", async () => {
    // REQ-27: the 'why' is the extension's own prose, so it reaches ctx.ui.notify
    // and never becomes a delivered message.
    const { pi, ctx } = await setUp("");

    await continueHandler(pi)("", ctx);

    const notices = ctx.ui.calls.filter((call) => call.method === "notify");
    assert.equal(notices.length, 1);
    // No session was opened, so nothing was delivered into model context at all.
    assert.equal(ctx.newSessionCalls.length, 0);
  });

  it("clears nothing when specflo is not installed", async () => {
    const fake = createFakeSpecflo(PAYLOAD);
    cleanups.push(() => fake.cleanup());
    const factory = await loadExtension(`${fake.bin}-does-not-exist`);
    const pi = createFakePi();
    factory(pi.api);
    const ctx = createFakeCtx({ cwd: fake.root });

    await continueHandler(pi)("", ctx);

    assert.equal(ctx.newSessionCalls.length, 0, "a missing binary clears nothing");
  });
});
