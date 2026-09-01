/**
 * Blocked-state surfacing (T-11, REQ-12).
 *
 * A blocking UI prompt opening in a served session lands an event line with
 * its kind and title, flips status.json to needs-attention, and reports
 * herdr blocked for a known pane; the prompt closing logs the close and
 * restores the prior state - working when a run was under way, idle
 * otherwise.
 */

import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";

import { registerControl } from "../src/control/mod.ts";
import { createControlHarness, type ControlHarness } from "./harness.ts";

const ENV_KEYS = [
  "SPECFLO_AGENT_STATE_DIR",
  "SPECFLO_AGENT_NAME",
  "SPECFLO_AGENT_MANAGED",
  "SPECFLO_AGENT_PANE",
  "SPECFLO_HERDR_BIN",
];

let saved: Record<string, string | undefined>;
let base: string;
let cwd: string;
let herdrRoot: string;
let herdrLog: string;

function createFakeHerdr(): string {
  herdrRoot = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-blocked-herdr-"));
  herdrLog = path.join(herdrRoot, "calls.log");
  const bin = path.join(herdrRoot, "herdr");
  fs.writeFileSync(bin, `#!/bin/sh\nprintf '%s\\n' "$*" >> ${JSON.stringify(herdrLog)}\n`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

function herdrReports(): Array<{ seq: number; state: string }> {
  if (!fs.existsSync(herdrLog)) return [];
  return fs
    .readFileSync(herdrLog, "utf8")
    .split("\n")
    .filter((line) => line.includes("report-agent"))
    .map((line) => ({
      seq: Number(line.match(/--seq (\d+)/)?.[1]),
      state: line.match(/--state (\S+)/)?.[1] ?? "",
    }))
    .sort((a, b) => a.seq - b.seq);
}

function waitFor(cond: () => boolean, timeoutMs = 5000): Promise<void> {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const tick = () => {
      if (cond()) return resolve();
      if (Date.now() > deadline) return reject(new Error("condition never held"));
      setTimeout(tick, 25);
    };
    tick();
  });
}

describe("blocked-state surfacing (T-11)", () => {
  beforeEach(() => {
    saved = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
    for (const key of ENV_KEYS) delete process.env[key];
    base = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-blocked-"));
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-bcwd-"));
    process.env.SPECFLO_AGENT_STATE_DIR = base;
    process.env.SPECFLO_HERDR_BIN = createFakeHerdr();
    process.env.SPECFLO_AGENT_NAME = "worker";
    process.env.SPECFLO_AGENT_MANAGED = "1";
  });

  afterEach(() => {
    for (const key of ENV_KEYS) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    fs.rmSync(base, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
    fs.rmSync(herdrRoot, { recursive: true, force: true });
  });

  async function served(): Promise<ControlHarness> {
    const harness = createControlHarness({ cwd });
    registerControl(harness.api);
    await harness.startSession();
    return harness;
  }

  function state(): string {
    return JSON.parse(fs.readFileSync(path.join(base, "worker", "status.json"), "utf8")).state;
  }

  function eventTypes(): any[] {
    return fs
      .readFileSync(path.join(base, "worker", "events.jsonl"), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  }

  test("a prompt during a run: needs-attention and herdr blocked, then working restored", async () => {
    process.env.SPECFLO_AGENT_PANE = "w1:p9";
    const harness = await served();
    try {
      await harness.emit({ type: "agent_start" });
      await harness.emit({
        type: "ui_prompt_start",
        reason: "ui_prompt",
        kind: "confirm",
        title: "Push to origin?",
      });
      assert.equal(state(), "needs-attention");
      const open = eventTypes().find((event) => event.type === "ui_prompt_start");
      assert.equal(open.kind, "confirm");
      assert.equal(open.title, "Push to origin?");

      await harness.emit({
        type: "ui_prompt_end",
        reason: "ui_prompt",
        kind: "confirm",
        title: "Push to origin?",
      });
      assert.equal(state(), "working", "the prior state resumes");
      const close = eventTypes().find((event) => event.type === "ui_prompt_end");
      assert.equal(close.kind, "confirm");

      await waitFor(() => herdrReports().length >= 3);
      assert.deepEqual(
        herdrReports().map((report) => report.state),
        ["working", "blocked", "working"],
      );
    } finally {
      await harness.shutdownSession();
    }
  });

  test("a prompt while idle restores idle at close", async () => {
    process.env.SPECFLO_AGENT_PANE = "w1:p9";
    const harness = await served();
    try {
      await harness.emit({ type: "ui_prompt_start", reason: "ui_prompt", kind: "input" });
      assert.equal(state(), "needs-attention");
      await harness.emit({ type: "ui_prompt_end", reason: "ui_prompt", kind: "input" });
      assert.equal(state(), "idle");
      await waitFor(() => herdrReports().length >= 2);
      assert.deepEqual(
        herdrReports().map((report) => report.state),
        ["blocked", "idle"],
      );
      // A title pi never supplied is simply absent from the line.
      const open = eventTypes().find((event) => event.type === "ui_prompt_start");
      assert.equal("title" in open, false);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("a settle during an open dialog surfaces at the close, not through it", async () => {
    // Review round 1, finding 3: the run ending mid-dialog must not
    // overwrite needs-attention, and the close must restore what the run
    // state is NOW (idle), not what it was at the open (working).
    process.env.SPECFLO_AGENT_PANE = "w1:p9";
    const harness = await served();
    try {
      await harness.emit({ type: "agent_start" });
      await harness.emit({ type: "ui_prompt_start", reason: "ui_prompt", kind: "confirm" });
      await harness.emit({ type: "agent_settled" });
      assert.equal(state(), "needs-attention", "the open dialog owns the surface");
      await harness.emit({ type: "ui_prompt_end", reason: "ui_prompt", kind: "confirm" });
      assert.equal(state(), "idle", "the close restores the run's current state");
      await waitFor(() => herdrReports().length >= 3);
      assert.deepEqual(
        herdrReports().map((report) => report.state),
        ["working", "blocked", "idle"],
      );
    } finally {
      await harness.shutdownSession();
    }
  });

  test("nested prompts: first open captures, last close restores", async () => {
    const harness = await served();
    try {
      await harness.emit({ type: "agent_start" });
      await harness.emit({ type: "ui_prompt_start", reason: "ui_prompt", kind: "confirm" });
      await harness.emit({ type: "ui_prompt_start", reason: "ui_prompt", kind: "input" });
      await harness.emit({ type: "ui_prompt_end", reason: "ui_prompt", kind: "input" });
      assert.equal(state(), "needs-attention", "one dialog is still open");
      await harness.emit({ type: "ui_prompt_end", reason: "ui_prompt", kind: "confirm" });
      assert.equal(state(), "working");
    } finally {
      await harness.shutdownSession();
    }
  });

  test("no pane still flips status but pushes nothing to herdr", async () => {
    const harness = await served();
    try {
      await harness.emit({ type: "ui_prompt_start", reason: "ui_prompt", kind: "select" });
      assert.equal(state(), "needs-attention");
      await harness.emit({ type: "ui_prompt_end", reason: "ui_prompt", kind: "select" });
      assert.equal(state(), "idle");
      await new Promise((resolve) => setTimeout(resolve, 200));
      assert.equal(fs.existsSync(herdrLog), false);
    } finally {
      await harness.shutdownSession();
    }
  });
});
