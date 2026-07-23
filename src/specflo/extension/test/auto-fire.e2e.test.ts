/**
 * The unattended fire end to end: a real pi session in a real auto run is
 * anchored once by the user-typed `/specflo-continue auto`, and the next armed
 * seam then replaces the session unattended - delivering the `specflo auto`
 * payload of the *current* phase into the new session with zero
 * extension_ui_request frames of any kind (T-16, REQ-29 / REQ-31 / REQ-13).
 *
 * Everything is real: arming comes from pi's own context estimate against a
 * low threshold, the auto run is started by the actual CLI before pi spawns,
 * the anchor is the ReplacedSessionContext captured at the user-typed clear,
 * and the seam is the project advancing brainstorm -> spec. The fire crosses
 * waitForIdle and a live newSession exactly as REQ-29 specifies - no command
 * dispatch, no user input.
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

const TIMEOUT = 180000;

/** The fixed marker opening every `specflo auto` payload. */
const BOOTSTRAP_MARKER = "== specflo auto-mode bootstrap ==";

/**
 * ~7.5k estimated tokens against the harness's 100k context window: past the
 * 5% threshold below, nowhere near pi's compaction reserve.
 */
const BIG_PROMPT = "x".repeat(30000);

describe("the unattended fire in a real pi session", () => {
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
      30000,
    );
    return (state as any).data.sessionId as string;
  };

  /**
   * Poll until pi reports a different sessionId than ``before``.
   *
   * A clear crosses waitForIdle, a subprocess and a session switch, so there
   * is no single event to wait on; the session change itself is the
   * observable outcome REQ-31 names.
   */
  const waitForSessionChange = async (
    pi: PiSession,
    before: string,
    label: string,
  ): Promise<string> => {
    const deadline = Date.now() + 60000;
    for (let poll = 0; Date.now() < deadline; poll += 1) {
      const current = await sessionId(pi, `${label}-${poll}`);
      if (current !== before) return current;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error(`the session never changed: ${label}`);
  };

  it("anchors on the typed command, then fires the next seam unattended", { timeout: TIMEOUT }, async () => {
    const provider: StubProvider = await startStubProvider([{ text: "ok" }]);
    cleanups.push(() => provider.close());
    const workspace: Workspace = createWorkspace(provider);
    cleanups.push(() => workspace.cleanup());
    initSpecfloProject(workspace);
    // The threshold must be in place before pi spawns: each session's
    // extension instance seeds it from its own status snapshot.
    fs.appendFileSync(
      path.join(workspace.project, ".specflo", "config.yaml"),
      "context_threshold_percent: 5\n",
    );
    // Start the auto run with the real CLI - pass one of the run pi joins.
    const firstPass = runSpecflo(workspace, ["auto"]);
    assert.ok(firstPass.includes(BOOTSTRAP_MARKER), "the CLI must emit the auto payload");
    const statusJson = JSON.parse(runSpecflo(workspace, ["status", "--json"])) as {
      dir: string;
      auto_run: { under_way: boolean };
    };
    assert.equal(statusJson.auto_run.under_way, true, "the run must be under way before pi spawns");
    const projectDir = statusJson.dir;

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    const settledCount = () =>
      pi.events.filter((event) => event.type === "agent_settled").length;

    const s0 = await sessionId(pi, "s0");

    // Turn 1 pushes context past the threshold; no specflo state has changed
    // since the cold-start baseline, so the armed poll declares nothing.
    pi.send({ id: "1", type: "prompt", message: BIG_PROMPT });
    await pi.waitFor(() => settledCount() >= 1, 60000);

    // The anchoring opt-in, exactly as a user types it: pass two of the run,
    // a session change, and the live anchor captured at the clear.
    pi.send({ id: "2", type: "prompt", message: "/specflo-continue auto" });
    const s1 = await waitForSessionChange(pi, s0, "anchor");
    await pi.waitFor(() => settledCount() >= 2, 60000); // the reseeded turn

    // Re-arm the fresh session: its own context must cross the threshold.
    pi.send({ id: "3", type: "prompt", message: BIG_PROMPT });
    await pi.waitFor(() => settledCount() >= 3, 60000);

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

    // The armed seam during the auto run: the anchored fire replaces the
    // session with no input of any kind.
    pi.send({ id: "4", type: "prompt", message: "hi" });
    const s2 = await waitForSessionChange(pi, s1, "fire");
    assert.notEqual(s2, s1, "the fire must replace the session");

    // The fire's payload is the *current* pass's: generated at phase spec,
    // where the anchoring pass's payload named brainstorm - so this proves a
    // fresh `specflo auto` report fired, not a replay.
    await pi
      .waitFor(
        () =>
          provider.requests.some((body) => {
            const text = promptText(body);
            return text.includes(BOOTSTRAP_MARKER) && text.includes("at the 'spec' phase");
          }),
        60000,
      )
      .catch(() => {
        throw new Error("the spec-phase auto payload never reached model context");
      });

    // Unattended means unattended: zero extension_ui_request frames of any
    // method across the whole run - no notify, no confirm, no select, no
    // input - for the anchoring clear and the fire alike.
    assert.deepEqual(pi.uiRequests(), []);

    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
    );
  });
});
