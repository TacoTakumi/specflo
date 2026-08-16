/**
 * Status segment wiring (T-02 RED / T-03 GREEN, REQ-04 / REQ-05 / REQ-06 /
 * REQ-07): how the extension applies the computed segment through
 * ctx.ui.setStatus.
 *
 * The behavioural cases drive the handlers against the fake pi and fake ctx:
 * every session_start reason re-applies the segment (pi wipes extension status
 * on session invalidation and /reload), a turn_end refreshes only when the
 * rendered text actually changed, and nothing-to-show clears with undefined.
 * The structural scans pin the display mechanism and the theme-token colour
 * rule over the extension source itself: exactly one setStatus keyed
 * 'specflo', no setFooter/setWidget, and no ANSI escape bytes.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, it } from "node:test";

import {
  clearSpecfloBin,
  createFakeCtx,
  createFakePi,
  createFakeSpecflo,
  loadExtension,
} from "./fake-ctx.ts";
import { createFixtureRepo, THREE_TASKS } from "./status-fixtures.ts";

const cleanups: Array<() => void> = [];
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup();
  clearSpecfloBin();
});

/** A loaded extension wired to a fake specflo, with a fixture repo at ctx.cwd. */
async function setUp(fixtureRoot: string) {
  const fake = createFakeSpecflo("");
  cleanups.push(() => fake.cleanup());
  const factory = await loadExtension(fake.bin);
  const pi = createFakePi();
  factory(pi.api);
  return { fake, pi, ctx: createFakeCtx({ cwd: fixtureRoot }) };
}

/** The setStatus calls the fake recorded, in order. */
function statusCalls(ctx: any): Array<{ key: string; value: string | undefined }> {
  return ctx.ui.calls.filter((c: { method: string }) => c.method === "setStatus").map(
    (c: { args: unknown[] }) => ({ key: c.args[0] as string, value: c.args[1] as string | undefined }),
  );
}

describe("status segment - session start re-applies for every reason (REQ-05)", () => {
  for (const reason of ["startup", "resume", "new", "fork", "reload"] as const) {
    it(`applies the segment once on a ${reason} start`, async () => {
      const root = createFixtureRepo({ phase: "execute", plan: THREE_TASKS });
      cleanups.push(() => fs.rmSync(root, { recursive: true, force: true }));
      const { pi, ctx } = await setUp(root);

      await pi.emit({ type: "session_start", reason }, ctx);

      const calls = statusCalls(ctx);
      assert.equal(calls.length, 1, "one setStatus per session start");
      assert.equal(calls[0].key, "specflo");
      assert.equal(calls[0].value, "demo T-02 1/3");
    });
  }
});

describe("status segment - turn-based refresh gated on change (REQ-06)", () => {
  it("re-sets nothing on a second turn_end when the artifacts are unchanged", async () => {
    const root = createFixtureRepo({ phase: "execute", plan: THREE_TASKS });
    cleanups.push(() => fs.rmSync(root, { recursive: true, force: true }));
    const { pi, ctx } = await setUp(root);

    await pi.emit({ type: "turn_end", message: {} }, ctx);
    await pi.emit({ type: "turn_end", message: {} }, ctx);

    assert.equal(statusCalls(ctx).length, 1, "the unchanged second turn_end performs no setStatus");
  });

  it("re-sets exactly once with the updated text when a task reaches done between turns", async () => {
    const root = createFixtureRepo({ phase: "execute", plan: THREE_TASKS });
    cleanups.push(() => fs.rmSync(root, { recursive: true, force: true }));
    const { pi, ctx } = await setUp(root);
    const planPath = path.join(root, "docs", "projects", "demo", "plan.md");

    await pi.emit({ type: "turn_end", message: {} }, ctx);
    const before = fs.readFileSync(planPath, "utf8");
    fs.writeFileSync(planPath, before.replace("- Progress: in_progress", "- Progress: done"));
    await pi.emit({ type: "turn_end", message: {} }, ctx);

    const calls = statusCalls(ctx);
    assert.equal(calls.length, 2, "exactly one setStatus for the change");
    assert.equal(calls[0].value, "demo T-02 1/3");
    assert.equal(calls[1].value, "demo 2/3", "the updated tally, wip id gone");
  });

  it("clears with undefined when there is nothing to show (REQ-02)", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-status-nothing-"));
    cleanups.push(() => fs.rmSync(root, { recursive: true, force: true }));
    const { pi, ctx } = await setUp(root);

    await pi.emit({ type: "turn_end", message: {} }, ctx);

    const calls = statusCalls(ctx);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].key, "specflo");
    assert.equal(calls[0].value, undefined);
  });
});

describe("status segment - structural scans over the extension source (REQ-04, REQ-07)", () => {
  const source = fs.readFileSync(path.join(import.meta.dirname, "..", "src", "index.ts"), "utf8");

  it("renders the segment through exactly one setStatus keyed 'specflo'", () => {
    assert.equal((source.match(/setStatus\("specflo"/g) ?? []).length, 1);
  });

  it("calls neither setFooter nor setWidget", () => {
    assert.equal((source.match(/setFooter\(|setWidget\(/g) ?? []).length, 0);
  });

  it("contains no ANSI escape bytes or escape-sequence literals", () => {
    assert.equal(source.indexOf("\u001b"), -1, "no literal ESC byte");
    assert.equal((source.match(/\\u001b|\\x1b|\\033/g) ?? []).length, 0, "no escape literals");
  });
});
