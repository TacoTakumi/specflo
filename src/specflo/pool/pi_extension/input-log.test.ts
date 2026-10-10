/**
 * The input-log extension against a fake pi: one line per input event, the
 * input left as it was, and nothing at all when no log is named.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import register, { INPUT_LOG_ENV } from "./input-log.ts";

type Handler = (event: any, ctx: any) => unknown;

function load(env: Record<string, string | undefined>) {
  const handlers = new Map<string, Handler[]>();
  const api = {
    on(event: string, handler: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), handler]);
    },
  };
  register(api as any, env);
  return {
    handlers,
    async input(source: string, text: string): Promise<unknown[]> {
      return this.fire("input", { source, text });
    },
    async fire(type: string, event: Record<string, unknown>): Promise<unknown[]> {
      const results = [];
      for (const handler of handlers.get(type) ?? []) {
        results.push(await handler({ type, ...event }, {}));
      }
      return results;
    },
  };
}

const lines = (path: string) =>
  readFileSync(path, "utf8").trim().split("\n").map((line) => JSON.parse(line));

describe("a pi run with an input log", () => {
  it("writes one line per input event with its source and text", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "input-log-")), "inputs.jsonl");
    const pi = load({ [INPUT_LOG_ENV]: path });

    await pi.input("extension", "continue the project");
    await pi.input("interactive", "stop that");

    const logged = lines(path);
    assert.deepEqual(logged.map(({ kind, source, text }) => ({ kind, source, text })), [
      { kind: "input", source: "extension", text: "continue the project" },
      { kind: "input", source: "interactive", text: "stop that" },
    ]);
    assert.ok(logged.every((line) => typeof line.ts === "string" && line.ts !== ""));
  });

  it("logs a bash command, a model change and a thinking change as interactive", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "input-log-")), "inputs.jsonl");
    const pi = load({ [INPUT_LOG_ENV]: path });
    const model = { provider: "llama-swap", id: "other" };

    await pi.fire("user_bash", { command: "ls", excludeFromContext: false, cwd: "/w" });
    await pi.fire("model_select", { model, previousModel: undefined, source: "cycle" });
    await pi.fire("model_select", { model, previousModel: undefined, source: "restore" });
    await pi.fire("thinking_level_select", { level: "low", previousLevel: "high" });

    assert.deepEqual(lines(path).map(({ kind, source, text }) => ({ kind, source, text })), [
      { kind: "bash", source: "interactive", text: "ls" },
      { kind: "model", source: "interactive", text: "llama-swap/other" },
      { kind: "thinking", source: "interactive", text: "low" },
    ]);
  });

  it("logs the slash commands pi marks as the operator's, and not the auto chain's", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "input-log-")), "inputs.jsonl");
    const pi = load({ [INPUT_LOG_ENV]: path });

    await pi.fire("session_compact", { reason: "manual", fromExtension: false });
    await pi.fire("session_compact", { reason: "manual", fromExtension: true });
    await pi.fire("session_compact", { reason: "threshold", fromExtension: false });
    for (const reason of ["startup", "new", "resume", "fork", "reload"]) {
      await pi.fire("session_start", { reason });
    }
    await pi.fire("session_tree", { newLeafId: "b", oldLeafId: "a", fromExtension: false });
    await pi.fire("session_tree", { newLeafId: "c", oldLeafId: "b", fromExtension: true });

    assert.deepEqual(lines(path).map(({ kind, source, text }) => ({ kind, source, text })), [
      { kind: "command", source: "interactive", text: "/compact" },
      { kind: "command", source: "interactive", text: "/resume" },
      { kind: "command", source: "interactive", text: "/fork" },
      { kind: "command", source: "interactive", text: "/reload" },
      { kind: "command", source: "interactive", text: "/tree" },
    ]);
  });

  it("leaves the input as it was", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "input-log-")), "inputs.jsonl");

    assert.deepEqual(await load({ [INPUT_LOG_ENV]: path }).input("rpc", "hello"), [undefined]);
  });

  it("does not fail the input when the log cannot be written", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "input-log-")), "no-such-dir", "inputs.jsonl");

    assert.deepEqual(await load({ [INPUT_LOG_ENV]: path }).input("interactive", "hello"), [undefined]);
    assert.equal(existsSync(path), false);
  });
});

describe("a pi run with no input log named", () => {
  it("registers no handler", () => {
    assert.equal(load({}).handlers.size, 0);
  });
});
