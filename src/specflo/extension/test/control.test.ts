/**
 * The control-surface module under the fake harness (T-01).
 *
 * First slice: the module registers session lifecycle hooks and the harness's
 * lifecycle triggers actually invoke them. The hooks are the mount points every
 * later control task (socket server, statefiles, commands) builds on.
 */

import { strict as assert } from "node:assert";
import { after, before, describe, test } from "node:test";

import { registerControl } from "../src/control/mod.ts";
import { createControlHarness } from "./harness.ts";

describe("control module - lifecycle hooks under the harness (T-01)", () => {
  // This suite is about hook wiring alone; the serve opt-out keeps the real
  // server core (and its state-layout writes) out of these lifecycle emits.
  let saved: string | undefined;
  before(() => {
    saved = process.env.SPECFLO_AGENT_SERVE;
    process.env.SPECFLO_AGENT_SERVE = "0";
  });
  after(() => {
    if (saved === undefined) delete process.env.SPECFLO_AGENT_SERVE;
    else process.env.SPECFLO_AGENT_SERVE = saved;
  });

  test("registers a session_start and a session_shutdown handler", () => {
    const harness = createControlHarness();
    registerControl(harness.api);
    assert.ok((harness.handlers.get("session_start") ?? []).length >= 1);
    assert.ok((harness.handlers.get("session_shutdown") ?? []).length >= 1);
  });

  test("the harness lifecycle triggers invoke the registered hooks", async () => {
    const harness = createControlHarness();
    registerControl(harness.api);
    await harness.startSession();
    await harness.shutdownSession();
    const events = harness.invoked.map((entry) => entry.event);
    assert.ok(events.includes("session_start"));
    assert.ok(events.includes("session_shutdown"));
  });
});
