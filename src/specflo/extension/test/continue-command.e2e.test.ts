/**
 * /specflo-continue end to end: a real pi session clears itself and reseeds the
 * active project's direct-continuation payload into the replacement session
 * (T-12, REQ-11 / REQ-14 / REQ-22 / REQ-27).
 *
 * The unit suite pins what the handler asks pi to do; this pins that pi does it -
 * a session change, and the `hook reseed --continue` payload byte-for-byte in
 * what the model is sent. The command arrives exactly as a user types it: a
 * prompt whose text is the slash command, which pi dispatches to the handler.
 */

import assert from "node:assert/strict";
import { after, describe, it } from "node:test";

import {
  createWorkspace,
  initSpecfloProject,
  runSpecflo,
  spawnPi,
  startStubProvider,
  type PiSession,
  type StubProvider,
  type Workspace,
} from "./rpc-harness.ts";

const TIMEOUT = 60000;

describe("/specflo-continue in a real pi session", () => {
  const cleanups: Array<() => void | Promise<void>> = [];
  after(async () => {
    for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
  });

  const setUp = async () => {
    const provider: StubProvider = await startStubProvider([{ text: "continuing" }]);
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

  /** The sessionId pi reports right now. The id disambiguates repeat reads. */
  const sessionId = async (pi: PiSession, id: string): Promise<string> => {
    pi.send({ id, type: "get_state" });
    const state = await pi.waitFor(
      (event) =>
        event.type === "response" &&
        (event as any).command === "get_state" &&
        (event as any).id === id,
      TIMEOUT - 5000,
    );
    return (state as any).data.sessionId as string;
  };

  it("clears the session and delivers the payload verbatim", { timeout: TIMEOUT }, async () => {
    const { provider, workspace } = await setUp();
    initSpecfloProject(workspace);
    const expected = runSpecflo(workspace, ["hook", "reseed", "--continue"]);
    assert.ok(expected.length > 0, "the fixture project should have a --continue payload");

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());

    const before = await sessionId(pi, "before");
    pi.send({ id: "1", type: "prompt", message: "/specflo-continue" });
    await pi.waitFor((event) => event.type === "agent_settled", TIMEOUT - 5000);
    const after_ = await sessionId(pi, "after");

    assert.notEqual(after_, before, "the session was never replaced");
    assert.ok(provider.requests.length >= 1, "the model was never called");
    const sent = promptText(provider.requests[0]);
    assert.ok(
      sent.includes(expected),
      `the --continue payload never reached model context.\n--- expected ---\n${expected}\n--- sent ---\n${sent}`,
    );
    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
    );
  });

  it("clears nothing and reports why with no active project", { timeout: TIMEOUT }, async () => {
    // No specflo project in the workspace: the command opens no new session and
    // sends the model nothing - only a notice reaches the user.
    const { provider, workspace } = await setUp();

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());

    const before = await sessionId(pi, "before");
    pi.send({ id: "1", type: "prompt", message: "/specflo-continue" });
    const notice = await pi.waitFor(
      (event) => event.type === "extension_ui_request" && (event as any).method === "notify",
      TIMEOUT - 5000,
    );
    const after_ = await sessionId(pi, "after");

    assert.equal((notice as any).method, "notify");
    assert.equal(after_, before, "no project means no clear");
    assert.equal(provider.requests.length, 0, "the model must not be called");
    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
    );
  });
});
