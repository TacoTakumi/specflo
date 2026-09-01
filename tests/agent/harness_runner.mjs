/**
 * Contract-suite runner: serves the v2 control module for real, no pi needed.
 *
 * Loads the extension's control module under its fake harness, starts a
 * session (which binds the control socket in SPECFLO_AGENT_STATE_DIR under
 * the SPECFLO_AGENT_NAME handshake), then simulates the agent the way
 * stub_pi.py does for the v1 host, driven by the same scenario keys:
 *
 *   mode    "reply" (default) - a delivered prompt runs one settled run
 *           "never_settle"    - the run starts and never settles
 *   reply   assistant text for a settled run (default "stub reply")
 *   capture path - append every delivered user message as a v1-shaped
 *           prompt frame, one JSON line each
 *
 * Usage: node harness_runner.mjs <scenario.json>
 * The process serves until killed; the parent owns its lifetime.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const extensionTest = path.join(here, "..", "..", "src", "specflo", "extension", "test");
const extensionSrc = path.join(here, "..", "..", "src", "specflo", "extension", "src");

const { createControlHarness } = await import(path.join(extensionTest, "harness.ts"));
const { registerControl } = await import(path.join(extensionSrc, "control", "mod.ts"));

const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const mode = scenario.mode ?? "reply";
const reply = scenario.reply ?? "stub reply";

function capture(frame) {
  if (scenario.capture) fs.appendFileSync(scenario.capture, `${JSON.stringify(frame)}\n`);
}

const harness = createControlHarness({ cwd: process.cwd() });

let runOpen = false;

async function runScript() {
  runOpen = true;
  harness.idle = false;
  await harness.emit({ type: "agent_start" });
  if (mode === "never_settle") return;
  const message = {
    role: "assistant",
    content: [{ type: "text", text: reply }],
    stopReason: "stop",
  };
  await harness.emit({ type: "message_start", message: { role: "assistant", content: [] } });
  await harness.emit({ type: "message_end", message });
  await harness.emit({ type: "agent_settled" });
  runOpen = false;
  harness.idle = true;
}

// Every delivered user message is recorded v1-capture-shaped; a delivery
// while no run is open starts one, exactly as a prompt does on real pi.
const deliver = harness.api.sendUserMessage.bind(harness.api);
harness.api.sendUserMessage = (content, options) => {
  deliver(content, options);
  const frame = { type: "prompt", message: content };
  if (options?.deliverAs) frame.streamingBehavior = options.deliverAs;
  capture(frame);
  if (!runOpen) void runScript();
};

registerControl(harness.api);
await harness.startSession();

// The control sockets are deliberately unref'd; this ref'd timer is what
// keeps the runner alive until the parent kills it.
setInterval(() => {}, 1 << 30);
