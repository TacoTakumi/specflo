/**
 * herdr state reporting from the extension (T-10, REQ-08).
 *
 * When the managed handshake carries a pane id, the control surface pushes
 * lifecycle transitions into herdr via `pane report-agent` - working at
 * agent_start, idle at agent_settled, each with a monotonic seq - and
 * releases the registration at shutdown. No pane id means zero herdr
 * invocations and zero errors; a failing herdr only lands a logged event.
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

/** A stand-in herdr: logs its argv, exits per the planted exit-code file. */
function createFakeHerdr(): string {
  herdrRoot = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-herdr-"));
  herdrLog = path.join(herdrRoot, "calls.log");
  const bin = path.join(herdrRoot, "herdr");
  const codeFile = path.join(herdrRoot, "exit-code");
  fs.writeFileSync(codeFile, "0");
  fs.writeFileSync(
    bin,
    `#!/bin/sh\nprintf '%s\\n' "$*" >> ${JSON.stringify(herdrLog)}\nexit $(cat ${JSON.stringify(codeFile)})\n`,
  );
  fs.chmodSync(bin, 0o755);
  return bin;
}

function herdrCalls(): string[] {
  if (!fs.existsSync(herdrLog)) return [];
  return fs.readFileSync(herdrLog, "utf8").split("\n").filter((line) => line !== "");
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

describe("herdr state reporting (T-10)", () => {
  beforeEach(() => {
    saved = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
    for (const key of ENV_KEYS) delete process.env[key];
    base = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-hstate-"));
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-hcwd-"));
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

  test("reports working then idle with a monotonic seq, and releases at shutdown", async () => {
    process.env.SPECFLO_AGENT_PANE = "w1:p9";
    const harness = await served();
    await harness.emit({ type: "agent_start" });
    await harness.emit({ type: "agent_settled" });
    await waitFor(() => herdrCalls().length >= 2);
    const reports = herdrCalls()
      .filter((call) => call.includes("report-agent"))
      .sort((a, b) => Number(a.match(/--seq (\d+)/)?.[1]) - Number(b.match(/--seq (\d+)/)?.[1]));
    assert.equal(reports.length, 2);
    assert.match(reports[0], /^pane report-agent w1:p9 /);
    assert.match(reports[0], /--agent worker/);
    assert.match(reports[0], /--state working/);
    assert.match(reports[0], /--seq 1\b/);
    assert.match(reports[1], /--state idle/);
    assert.match(reports[1], /--seq 2\b/);

    await harness.shutdownSession();
    await waitFor(() => herdrCalls().some((call) => call.includes("release-agent")));
    const release = herdrCalls().find((call) => call.includes("release-agent"));
    assert.match(release!, /^pane release-agent w1:p9 /);
    assert.match(release!, /--agent worker/);
  });

  test("the discovery record carries the handshake pane id", async () => {
    // So an ownership-aware stop can release the registration even when the
    // session dies without its own shutdown (review round 1, finding 2).
    process.env.SPECFLO_AGENT_PANE = "w1:p9";
    const harness = await served();
    try {
      const status = JSON.parse(
        fs.readFileSync(path.join(base, "worker", "status.json"), "utf8"),
      );
      assert.equal(status.herdr_pane, "w1:p9");
    } finally {
      await harness.shutdownSession();
    }
  });

  test("no pane id means zero herdr invocations and zero errors", async () => {
    const harness = await served();
    await harness.emit({ type: "agent_start" });
    await harness.emit({ type: "agent_settled" });
    await harness.shutdownSession();
    // Give any stray spawn a moment to land, then assert silence.
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.deepEqual(herdrCalls(), []);
  });

  test("a failing herdr lands a logged event and nothing else breaks", async () => {
    process.env.SPECFLO_AGENT_PANE = "w1:p9";
    fs.writeFileSync(path.join(herdrRoot, "exit-code"), "1");
    const harness = await served();
    await harness.emit({ type: "agent_start" });
    const eventsPath = path.join(base, "worker", "events.jsonl");
    await waitFor(() =>
      fs
        .readFileSync(eventsPath, "utf8")
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line))
        .some((event) => event.type === "host_error" && /herdr/.test(event.error)),
    );
    // The run itself is untouched: settle still mirrors and status still moves.
    await harness.emit({ type: "agent_settled" });
    const status = JSON.parse(
      fs.readFileSync(path.join(base, "worker", "status.json"), "utf8"),
    );
    assert.equal(status.state, "idle");
    await harness.shutdownSession();
  });
});
