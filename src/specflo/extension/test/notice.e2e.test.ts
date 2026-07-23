/**
 * The attended notice end to end: a real pi session at an armed seam emits one
 * notify ui frame and nothing else - no dialog, no session change - and the
 * notice text never enters model context (T-13, REQ-09 / REQ-10).
 *
 * Arming is real: the stub provider reports no token usage, so pi falls back
 * to its chars/4 context estimate, and one large prompt pushes usage over a
 * low threshold written into the project's config before pi starts. The seam
 * is real too: between two prompts the test advances the project
 * brainstorm -> spec with the actual CLI, so what the armed poll sees is what
 * `specflo status --json` reports.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
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

const TIMEOUT = 120000;

/**
 * ~7.5k estimated tokens against the harness's 100k context window: past the
 * 5% threshold below, nowhere near pi's compaction reserve.
 */
const BIG_PROMPT = "x".repeat(30000);

describe("attended notice in a real pi session", () => {
  const cleanups: Array<() => void | Promise<void>> = [];
  after(async () => {
    for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
  });

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

  it("notifies once at an armed seam, opens nothing, clears nothing", { timeout: TIMEOUT }, async () => {
    const provider: StubProvider = await startStubProvider([{ text: "ok" }]);
    cleanups.push(() => provider.close());
    const workspace: Workspace = createWorkspace(provider);
    cleanups.push(() => workspace.cleanup());
    initSpecfloProject(workspace);
    // The threshold must be in place before pi spawns: the extension seeds it
    // from the one cold-start status snapshot.
    fs.appendFileSync(
      path.join(workspace.project, ".specflo", "config.yaml"),
      "context_threshold_percent: 5\n",
    );
    const projectDir = (
      JSON.parse(runSpecflo(workspace, ["status", "--json"])) as { dir: string }
    ).dir;

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    const settledCount = () =>
      pi.events.filter((event) => event.type === "agent_settled").length;

    const before = await sessionId(pi, "before");

    // Turn 1 pushes context past the threshold; no specflo state has changed
    // since the cold-start baseline, so the armed poll declares nothing.
    pi.send({ id: "1", type: "prompt", message: BIG_PROMPT });
    await pi.waitFor(() => settledCount() >= 1, TIMEOUT - 5000);

    // The seam, made of real state: satisfy the brainstorm gate and advance,
    // so the next poll reports phase spec against the brainstorm baseline.
    runSpecflo(workspace, ["decision", "add", "--text", "Keep it simple"]);
    const brainstorm = path.join(projectDir, "brainstorm.md");
    fs.writeFileSync(
      brainstorm,
      fs
        .readFileSync(brainstorm, "utf8")
        .replace("## Out of scope / Deferred", "## Out of scope / Deferred\n\n- Everything else."),
    );
    runSpecflo(workspace, ["advance"]);

    // Turn 2: the armed poll sees the phase change and notifies, once.
    pi.send({ id: "2", type: "prompt", message: "hi" });
    const notice = await pi.waitFor(
      (event) => event.type === "extension_ui_request" && (event as any).method === "notify",
      TIMEOUT - 5000,
    );
    await pi.waitFor(() => settledCount() >= 2, TIMEOUT - 5000);

    // Turn 3: the snapshot is unchanged, so the seam is spent - no re-notify
    // (REQ-10). The absence is read as a bounded timeout on a second frame.
    pi.send({ id: "3", type: "prompt", message: "hi again" });
    await pi.waitFor(() => settledCount() >= 3, TIMEOUT - 5000);
    await assert.rejects(
      pi.waitFor(() => pi.uiRequests("notify").length >= 2, 4000),
      /timed out/,
      "a second armed turn over an unchanged snapshot must not re-notify",
    );

    // One notify, zero dialogs (REQ-09).
    assert.equal(pi.uiRequests("notify").length, 1);
    assert.equal(pi.uiRequests("confirm").length, 0);
    assert.equal(pi.uiRequests("select").length, 0);
    assert.equal(pi.uiRequests("input").length, 0);

    // The notice names the usage, the seam, and the exact command.
    const message = (notice as any).message as string;
    assert.match(message, /\d+%/, "the notice names the current context usage");
    assert.match(message, /spec/, "the notice names the seam that fired");
    assert.match(message, /\/specflo-continue/, "the notice names the command to run");

    // Passive: the session was never replaced.
    const after_ = await sessionId(pi, "after");
    assert.equal(after_, before, "the attended notice must not clear the session");

    // The notice text never reaches model context: no request the provider saw
    // contains it.
    for (const body of provider.requests) {
      assert.ok(
        !promptText(body).includes(message),
        "the notice text must never enter model context",
      );
    }

    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
    );
  });
});
