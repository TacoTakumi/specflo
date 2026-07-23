/**
 * Cold start: fetch the reseed payload at session_start, inject it verbatim at
 * the next before_agent_start (T-09, REQ-17 / REQ-32 / REQ-27).
 *
 * These drive the handlers directly against a fake pi and a fake `specflo`
 * executable, so each case pins one input a live run cannot be steered into on
 * demand: a session that starts for each of pi's five reasons, a directory with
 * no active project, a second turn, a specflo that is not installed.
 */

import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  loadExtension,
} from "./fake-ctx.ts";

const PAYLOAD =
  "== specflo next step ==\nCurrent phase: execute. Next: work T-09.\n" +
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
  return { fake, pi, ctx: createFakeCtx({ cwd: fake.root }) };
}

describe("cold start", () => {
  for (const reason of ["startup", "resume"] as const) {
    it(`injects the payload verbatim after a ${reason} session start`, async () => {
      const { fake, pi, ctx } = await setUp(PAYLOAD);

      await pi.emit({ type: "session_start", reason }, ctx);
      const result = (await pi.emit({ type: "before_agent_start", prompt: "hi" }, ctx)) as any;

      assert.equal(result.message.content, PAYLOAD);
      assert.equal(
        Buffer.compare(Buffer.from(result.message.content), Buffer.from(PAYLOAD)),
        0,
        "the injected message must be the CLI's stdout byte for byte",
      );
      // reseed for the payload, then status --json to seed the arming snapshot.
      assert.equal(fake.invocations().length, 2);
    });
  }

  it("fetches the ask-first payload, without the direct-continuation flag", async () => {
    const { fake, pi, ctx } = await setUp(PAYLOAD);

    await pi.emit({ type: "session_start", reason: "startup" }, ctx);

    // The reseed carries no direct-continuation flag; the status read that
    // follows it seeds the arming threshold from the cold-start snapshot.
    assert.deepEqual(fake.invocations(), ["hook reseed", "status --json"]);
  });

  it("injects nothing when there is no active project", async () => {
    // `specflo hook reseed` prints nothing outside a project, or with no active
    // one - and nothing is what the session should be told.
    const { pi, ctx } = await setUp("");

    await pi.emit({ type: "session_start", reason: "startup" }, ctx);
    const result = await pi.emit({ type: "before_agent_start", prompt: "hi" }, ctx);

    assert.equal(result, undefined);
  });

  it("injects once, on the first turn only", async () => {
    const { pi, ctx } = await setUp(PAYLOAD);

    await pi.emit({ type: "session_start", reason: "startup" }, ctx);
    const first = (await pi.emit({ type: "before_agent_start", prompt: "one" }, ctx)) as any;
    const second = await pi.emit({ type: "before_agent_start", prompt: "two" }, ctx);

    assert.equal(first.message.content, PAYLOAD);
    assert.equal(second, undefined);
  });

  for (const reason of ["new", "fork", "reload"] as const) {
    it(`fetches no payload on a ${reason} session start`, async () => {
      // Those sessions are opened by something that already knows what it wants
      // next, or are a resource refresh - not a session coming up cold. Each
      // still seeds its own arming state: the closure is per-session, so the
      // `new` session a clear opens would otherwise never re-arm (T-16).
      const { fake, pi, ctx } = await setUp(PAYLOAD);

      await pi.emit({ type: "session_start", reason }, ctx);
      const result = await pi.emit({ type: "before_agent_start", prompt: "hi" }, ctx);

      assert.deepEqual(fake.invocations(), ["status --json"]);
      assert.equal(result, undefined);
    });
  }

  it("stays out of the way when specflo is not installed", async () => {
    const fake = createFakeSpecflo(PAYLOAD);
    cleanups.push(() => fake.cleanup());
    const factory = await loadExtension(`${fake.bin}-does-not-exist`);
    const pi = createFakePi();
    factory(pi.api);
    const ctx = createFakeCtx({ cwd: fake.root });

    await pi.emit({ type: "session_start", reason: "startup" }, ctx);
    const result = await pi.emit({ type: "before_agent_start", prompt: "hi" }, ctx);

    assert.equal(result, undefined, "a missing binary must leave the session untouched");
  });

  it("registers the continue command and no tool", async () => {
    // REQ-15 / REQ-03 at the registration level; the guard suite covers the
    // source. The one command is /specflo-continue (T-12); there is no tool and
    // no tool_call handler at all, and no event handler beyond the three below.
    const { pi } = await setUp(PAYLOAD);

    assert.deepEqual(pi.tools, []);
    assert.deepEqual([...pi.commands.keys()], ["specflo-continue"]);
    assert.deepEqual(
      [...pi.handlers.keys()].sort(),
      ["before_agent_start", "session_start", "turn_end"],
    );
  });

  it("puts no prose of its own into the message", async () => {
    // REQ-27: the payload is the CLI's stdout and nothing else - not a prefix,
    // not a wrapper, not a header the extension thought would help.
    const { pi, ctx } = await setUp(PAYLOAD);

    await pi.emit({ type: "session_start", reason: "startup" }, ctx);
    const result = (await pi.emit({ type: "before_agent_start", prompt: "hi" }, ctx)) as any;

    assert.equal(result.message.content.length, PAYLOAD.length);
    assert.equal(result.message.display, false);
  });
});
