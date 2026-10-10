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
      const results = [];
      for (const handler of handlers.get("input") ?? []) {
        results.push(await handler({ type: "input", source, text }, {}));
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
    assert.deepEqual(logged.map(({ source, text }) => ({ source, text })), [
      { source: "extension", text: "continue the project" },
      { source: "interactive", text: "stop that" },
    ]);
    assert.ok(logged.every((line) => typeof line.ts === "string" && line.ts !== ""));
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
