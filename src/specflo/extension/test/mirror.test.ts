/**
 * Statefile parity and event mirroring under the fake harness (T-03, REQ-07).
 *
 * The control surface mirrors the session's run events into the same
 * append-only events.jsonl the v1 host writes - RPC-shaped lines, each with a
 * ts field - and drives status.json through the v1 lifecycle transitions:
 * working at agent_start, idle at agent_settled. Token-level update events
 * (message_update, tool_execution_update) are deliberately not mirrored.
 */

import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";

import { registerControl } from "../src/control/mod.ts";
import { createControlHarness, type ControlHarness } from "./harness.ts";

let savedStateDir: string | undefined;
let base: string;
let cwd: string;

async function servedHarness(): Promise<ControlHarness> {
  const harness = createControlHarness({ cwd });
  registerControl(harness.api);
  await harness.startSession();
  return harness;
}

function recordDir(): string {
  return path.join(base, path.basename(cwd));
}

function readEvents(): any[] {
  return fs
    .readFileSync(path.join(recordDir(), "events.jsonl"), "utf8")
    .split("\n")
    .filter((line) => line !== "")
    .map((line) => JSON.parse(line));
}

function readState(): string {
  return JSON.parse(fs.readFileSync(path.join(recordDir(), "status.json"), "utf8")).state;
}

describe("event mirroring and lifecycle (T-03)", () => {
  // Scoped to this describe: the whole suite runs in one process under the
  // test/index.js barrel, where a top-level hook would leak onto every other
  // file's tests (and theirs onto these).
  beforeEach(() => {
    savedStateDir = process.env.SPECFLO_AGENT_STATE_DIR;
    base = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-mirror-"));
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-mcwd-"));
    process.env.SPECFLO_AGENT_STATE_DIR = base;
  });

  afterEach(() => {
    if (savedStateDir === undefined) delete process.env.SPECFLO_AGENT_STATE_DIR;
    else process.env.SPECFLO_AGENT_STATE_DIR = savedStateDir;
    fs.rmSync(base, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  });

  test("a scripted run lands agent_start then agent_settled, working then idle", async () => {
    const harness = await servedHarness();
    try {
      assert.equal(readState(), "idle");
      await harness.emit({ type: "agent_start" });
      assert.equal(readState(), "working");
      await harness.emit({ type: "agent_settled" });
      assert.equal(readState(), "idle");
      const types = readEvents().map((event) => event.type);
      // control_start is the server's own T-05 transition note.
      assert.deepEqual(types, ["control_start", "agent_start", "agent_settled"]);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("message and tool lines are mirrored verbatim, each with a ts field", async () => {
    const harness = await servedHarness();
    try {
      const message = { role: "assistant", content: "hello" };
      await harness.emit({ type: "agent_start" });
      await harness.emit({ type: "message_start", message });
      await harness.emit({ type: "tool_execution_start", toolCallId: "t1", toolName: "bash", args: { cmd: "ls" } });
      await harness.emit({ type: "tool_execution_end", toolCallId: "t1", toolName: "bash", result: "ok", isError: false });
      await harness.emit({ type: "message_end", message });
      await harness.emit({ type: "agent_end", messages: [message] });
      await harness.emit({ type: "agent_settled" });

      const events = readEvents();
      assert.deepEqual(
        events.map((event) => event.type),
        [
          "control_start",
          "agent_start",
          "message_start",
          "tool_execution_start",
          "tool_execution_end",
          "message_end",
          "agent_end",
          "agent_settled",
        ],
      );
      for (const event of events) {
        assert.equal(typeof event.ts, "string", `missing ts on ${event.type}`);
      }
      const messageEnd = events.find((event) => event.type === "message_end");
      assert.deepEqual(messageEnd.message, message);
      const toolStart = events.find((event) => event.type === "tool_execution_start");
      assert.equal(toolStart.toolName, "bash");
      assert.deepEqual(toolStart.args, { cmd: "ls" });
    } finally {
      await harness.shutdownSession();
    }
  });

  test("token-level update events are not mirrored", async () => {
    const harness = await servedHarness();
    try {
      await harness.emit({ type: "agent_start" });
      await harness.emit({ type: "message_update", message: {}, assistantMessageEvent: {} });
      await harness.emit({ type: "tool_execution_update", toolCallId: "t1", toolName: "bash", args: {}, partialResult: "" });
      await harness.emit({ type: "agent_settled" });
      assert.deepEqual(
        readEvents().map((event) => event.type),
        ["control_start", "agent_start", "agent_settled"],
      );
    } finally {
      await harness.shutdownSession();
    }
  });

  test("status.json keeps the v1 core fields through a transition", async () => {
    const harness = await servedHarness();
    try {
      await harness.emit({ type: "agent_start" });
      const status = JSON.parse(fs.readFileSync(path.join(recordDir(), "status.json"), "utf8"));
      for (const key of [
        "name",
        "state",
        "host_pid",
        "pi_pid",
        "context_percent",
        "herdr_workspace",
        "herdr_tab",
        "herdr_pane",
        "last_activity",
      ]) {
        assert.ok(key in status, `missing v1 core field ${key}`);
      }
      assert.equal(status.state, "working");
      assert.equal(status.pi_pid, process.pid);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("with the opt-out set a run mirrors nothing and throws nothing", async () => {
    process.env.SPECFLO_AGENT_SERVE = "0";
    try {
      const harness = createControlHarness({ cwd });
      registerControl(harness.api);
      await harness.startSession();
      await harness.emit({ type: "agent_start" });
      await harness.emit({ type: "agent_settled" });
      await harness.shutdownSession();
      assert.deepEqual(fs.readdirSync(base), []);
    } finally {
      delete process.env.SPECFLO_AGENT_SERVE;
    }
  });

  test("events.jsonl survives shutdown; socket and record do not", async () => {
    // v1 retains the event log after stop; the discovery record is what a
    // clean exit removes (REQ-02).
    const harness = await servedHarness();
    await harness.emit({ type: "agent_start" });
    await harness.emit({ type: "agent_settled" });
    await harness.shutdownSession();
    assert.ok(fs.existsSync(path.join(recordDir(), "events.jsonl")));
    assert.equal(fs.existsSync(path.join(recordDir(), "status.json")), false);
    assert.equal(fs.existsSync(path.join(recordDir(), "sock")), false);
  });
});
