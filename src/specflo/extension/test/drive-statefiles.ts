/**
 * Statefile fixture driver for the Python parity test (T-03, REQ-07).
 *
 * Runs the control module under the fake harness through one scripted run -
 * session_start, agent_start, one assistant message, one tool call,
 * agent_end, agent_settled - and exits WITHOUT session_shutdown, leaving the
 * state dir populated the way a live served session's dir looks: sock (dead
 * once this process exits), status.json (idle), events.jsonl.
 *
 * Invoked as: node test/drive-statefiles.ts
 * with SPECFLO_AGENT_STATE_DIR pointing at the base dir to populate and the
 * cwd naming the derived identity. tests/agent/test_v2_statefiles.py then
 * reads the files with the v1 statefiles reader and CLI code paths.
 *
 * Wrapped in a node:test case so that a bare `node --test`, whose discovery
 * sweeps everything under test/, reports an explicit skip instead of running
 * the driver against an unset state dir. The skip condition is evaluated at
 * registration, before any suite in a shared process could set the variable.
 */

import { test } from "node:test";

import { registerControl } from "../src/control/mod.ts";
import { createControlHarness } from "./harness.ts";

test(
  "drive one scripted run into the state layout",
  { skip: process.env.SPECFLO_AGENT_STATE_DIR ? false : "SPECFLO_AGENT_STATE_DIR not set" },
  async () => {
    const harness = createControlHarness({ cwd: process.cwd() });
    registerControl(harness.api);

    const message = { role: "assistant", content: [{ type: "text", text: "all done" }] };

    await harness.startSession();
    await harness.emit({ type: "agent_start" });
    await harness.emit({ type: "message_start", message });
    await harness.emit({
      type: "tool_execution_start",
      toolCallId: "call-1",
      toolName: "bash",
      args: { cmd: "true" },
    });
    await harness.emit({
      type: "tool_execution_end",
      toolCallId: "call-1",
      toolName: "bash",
      result: "",
      isError: false,
    });
    await harness.emit({ type: "message_end", message });
    await harness.emit({ type: "agent_end", messages: [message] });
    await harness.emit({ type: "agent_settled" });
  },
);
