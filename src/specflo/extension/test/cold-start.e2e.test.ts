/**
 * Cold start, end to end: a real pi session in a real specflo repo carries the
 * reseed payload into model context, byte for byte (T-09, REQ-17 / REQ-27).
 *
 * The unit suite pins what the handler returns; this pins that pi does what the
 * handler asked. The assertion is made where it counts - on the request body the
 * provider receives - so it proves the payload reached the model rather than
 * merely that a handler returned one.
 */

import assert from "node:assert/strict";
import { after, describe, it } from "node:test";

import {
  createWorkspace,
  initSpecfloProject,
  runSpecflo,
  spawnPi,
  startStubProvider,
  type StubProvider,
  type Workspace,
} from "./rpc-harness.ts";

const TIMEOUT = 60000;

describe("cold start in a real pi session", () => {
  const cleanups: Array<() => void | Promise<void>> = [];
  after(async () => {
    for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
  });

  const setUp = async () => {
    const provider: StubProvider = await startStubProvider([{ text: "acknowledged" }]);
    cleanups.push(() => provider.close());
    const workspace: Workspace = createWorkspace(provider);
    cleanups.push(() => workspace.cleanup());
    return { provider, workspace };
  };

  /** Every text fragment the provider was sent, across all message shapes. */
  const promptText = (body: string): string => {
    const parsed = JSON.parse(body) as { messages: Array<{ content: unknown }> };
    return parsed.messages
      .map((message) =>
        typeof message.content === "string"
          ? message.content
          : Array.isArray(message.content)
            ? message.content
                .map((part: any) => (typeof part?.text === "string" ? part.text : ""))
                .join("")
            : "",
      )
      .join("\n");
  };

  it("delivers the hook reseed payload verbatim", { timeout: TIMEOUT }, async () => {
    const { provider, workspace } = await setUp();
    initSpecfloProject(workspace);
    const expected = runSpecflo(workspace, ["hook", "reseed"]);
    assert.ok(expected.length > 0, "the fixture project should have a reseed payload");

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    pi.send({ id: "1", type: "prompt", message: "hello" });
    await pi.waitFor((event) => event.type === "agent_settled", TIMEOUT - 5000);

    assert.equal(provider.requests.length >= 1, true, "the model was never called");
    const sent = promptText(provider.requests[0]);
    assert.ok(
      sent.includes(expected),
      `the reseed payload never reached model context.\n--- expected ---\n${expected}\n--- sent ---\n${sent}`,
    );
    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
    );
  });

  it("delivers nothing when there is no active project", { timeout: TIMEOUT }, async () => {
    // Same session, same extension - only the project is missing. Nothing about
    // specflo may appear in what the model is sent.
    const { provider, workspace } = await setUp();

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    pi.send({ id: "1", type: "prompt", message: "hello" });
    await pi.waitFor((event) => event.type === "agent_settled", TIMEOUT - 5000);

    const sent = promptText(provider.requests[0]);
    assert.ok(!sent.includes("# Checkpoint"), "a checkpoint leaked into an unrelated session");
    assert.ok(!sent.includes("specflo status"), "specflo prose leaked into an unrelated session");
    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
    );
  });
});
