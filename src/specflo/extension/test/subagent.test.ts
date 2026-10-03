/**
 * A pi-subagents child session: the extension does nothing in it.
 *
 * pi-subagents binds every extension to the child session it makes, so the
 * child gets its own session_start and turn_end. The child has a task from its
 * parent and no user. A reseed payload tells it to stop and ask a user, and a
 * seam clear would end the parent's task, so both stay off.
 *
 * The child is known by its header: pi-subagents writes the parent's session
 * id to `parentSession`. A fork has the parent's session file path there and
 * keeps the cold-start reseed.
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

const PAYLOAD = "You are resuming a specflo project.\n# Checkpoint - demo\n";

/** The header value pi-subagents writes: the parent's session id. */
const PARENT_ID = "01a101cb-52a8-7422-897f-94f1400f2aba";
/** The header value a fork carries: the parent's session file. */
const PARENT_FILE = "/home/u/.pi/agent/sessions/--p--/2026-10-03T12-44-24-616Z_01a101cb.jsonl";

/** A status --json snapshot with the arming threshold and a done count. */
function status(done: number): string {
  const total = 15;
  return JSON.stringify({
    phase: "execute",
    context_threshold_percent: 75,
    auto_run: { under_way: true },
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

/** A loaded extension wired to a fake specflo that replays ``stdout``. */
async function setUp(stdout: string) {
  const fake = createFakeSpecflo(stdout);
  cleanups.push(() => fake.cleanup());
  const factory = await loadExtension(fake.bin);
  const pi = createFakePi();
  factory(pi.api);
  return { fake, pi };
}

describe("a pi-subagents child session", () => {
  for (const reason of ["startup", "resume"] as const) {
    it(`runs no specflo command and injects nothing after a ${reason} session start`, async () => {
      const { fake, pi } = await setUp(PAYLOAD);
      const ctx = createFakeCtx({ cwd: fake.root, parentSession: PARENT_ID });

      await pi.emit({ type: "session_start", reason }, ctx);
      const result = await pi.emit({ type: "before_agent_start", prompt: "the task" }, ctx);

      assert.equal(result, undefined, "the child's first turn must carry no reseed payload");
      assert.deepEqual(fake.invocations(), []);
    });
  }

  it("gives no notice and no abort at an armed seam", async () => {
    // Seeded through a user session so the threshold and the baseline exist:
    // the turn_end guard is then the one thing that keeps the child silent.
    const { fake, pi } = await setUp(status(11));
    await pi.emit({ type: "session_start", reason: "startup" }, createFakeCtx({ cwd: fake.root }));
    const seeded = fake.invocations().length;

    fake.setStdout(status(12));
    const child = createFakeCtx({
      cwd: fake.root,
      parentSession: PARENT_ID,
      contextUsage: { tokens: 160000, contextWindow: 200000, percent: 80 },
    });
    await pi.emit({ type: "turn_end", turnIndex: 0, message: {}, toolResults: [] }, child);

    assert.equal(child.ui.calls.filter((call) => call.method === "notify").length, 0);
    assert.equal(child.abortCalls.length, 0);
    assert.equal(fake.invocations().length, seeded, "the child's turn must not poll specflo");
  });
});

describe("a fork", () => {
  it("still gets the reseed: its parentSession is a file path", async () => {
    const { fake, pi } = await setUp(PAYLOAD);
    const ctx = createFakeCtx({ cwd: fake.root, parentSession: PARENT_FILE });

    await pi.emit({ type: "session_start", reason: "resume" }, ctx);
    const result = (await pi.emit({ type: "before_agent_start", prompt: "hi" }, ctx)) as any;

    assert.equal(result.message.content, PAYLOAD);
  });
});
