/**
 * The sail-on, reproduced end to end at a task-done seam (T-02, REQ-02 /
 * REQ-05): during a real auto run, a run that chains bash tool calls marks a
 * task done mid-run via the real CLI. The armed, anchored seam must end the
 * run so the parked fire lands - the session replaced, the fresh `specflo
 * auto` payload delivered verbatim into the new session, zero
 * extension_ui_request frames.
 *
 * Everything is real, as in auto-fire.e2e.test.ts: the workspace project is
 * driven to the execute phase through the actual CLI gates, the auto run is
 * started by the CLI before pi spawns, the anchor is captured by the
 * user-typed `/specflo-continue auto`, and the seam is `specflo task done`
 * run by pi's own bash tool. The scripted provider then keeps serving bash
 * turns forever, so a run the seam does not end never goes idle: on code
 * without the abort this test fails by timeout with the session never
 * changing - the sail-on.
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

/**
 * Drive the workspace's demo project brainstorm -> spec -> plan -> execute
 * through the real CLI, satisfying each phase gate the way the phase skills
 * would: decisions, requirements and tasks via their commands; the required
 * free-text sections filled in place.
 */
function driveToExecute(workspace: Workspace): void {
  const projectDir = path.join(workspace.project, "docs", "projects", "demo-thing");
  const fillSection = (file: string, heading: string, body: string) => {
    const p = path.join(projectDir, file);
    fs.writeFileSync(p, fs.readFileSync(p, "utf8").replace(heading, `${heading}\n\n${body}`));
  };

  runSpecflo(workspace, ["decision", "add", "--text", "Keep it simple"]);
  fillSection("brainstorm.md", "## Out of scope / Deferred", "- Everything else.");
  runSpecflo(workspace, ["advance"]);

  runSpecflo(workspace, ["spec", "start"]);
  runSpecflo(workspace, [
    "requirement", "add",
    "--text", "Demo does the thing",
    "--acceptance", "the thing happens",
  ]);
  fillSection("spec.md", "### In scope", "- The thing.");
  fillSection("spec.md", "### Out of scope", "- Everything else.");
  runSpecflo(workspace, ["advance"]);

  runSpecflo(workspace, ["plan", "start"]);
  for (const title of ["Do the first thing", "Do the second thing", "Do the third thing"]) {
    runSpecflo(workspace, [
      "task", "add",
      "--text", title,
      "--acceptance", "it is done",
      "--verify", "echo verified",
      "--from", "REQ-01",
    ]);
  }
  runSpecflo(workspace, ["advance"]);
}

describe("the task-done seam mid-run ends the run and fires the continuation", () => {
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
   * The run the seam must end is scripted to never go idle on its own, so on
   * unfixed code this is exactly where the test dies: the fire stays parked
   * on waitForIdle forever and the session never changes.
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

  it("replaces the session at the seam with the fresh auto payload", { timeout: TIMEOUT }, async () => {
    // The scripted model: three plain turns (the arming turn, the anchoring
    // clear's reseeded turn, the re-arm turn), then the run under test - a
    // bash call that marks T-01 done through the real CLI, then bash turns
    // that repeat forever. A run the seam fails to end here sails on
    // indefinitely; only the abort can end it.
    const provider: StubProvider = await startStubProvider([
      { text: "ok" },
      { text: "ok" },
      { text: "ok" },
      {
        toolCall: {
          name: "bash",
          arguments: { command: "specflo task start T-01 && specflo task done T-01" },
        },
      },
      { toolCall: { name: "bash", arguments: { command: "echo sailing on" } } },
    ]);
    cleanups.push(() => provider.close());
    const workspace: Workspace = createWorkspace(provider);
    cleanups.push(() => workspace.cleanup());
    initSpecfloProject(workspace);
    driveToExecute(workspace);
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
      phase: string;
      auto_run: { under_way: boolean };
    };
    assert.equal(statusJson.phase, "execute", "the project must reach the execute phase");
    assert.equal(statusJson.auto_run.under_way, true, "the run must be under way before pi spawns");

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    const settledCount = () =>
      pi.events.filter((event) => event.type === "agent_settled").length;

    const s0 = await sessionId(pi, "s0");

    // Turn 1 pushes context past the threshold; no specflo state has changed
    // since the cold-start baseline, so the armed poll declares nothing.
    pi.send({ id: "1", type: "prompt", message: BIG_PROMPT });
    await pi.waitFor(() => settledCount() >= 1, 60000);

    // The anchoring opt-in, exactly as a user types it: a session change and
    // the live anchor captured at the clear. Its payload still names T-01.
    pi.send({ id: "2", type: "prompt", message: "/specflo-continue auto" });
    const s1 = await waitForSessionChange(pi, s0, "anchor");
    await pi.waitFor(() => settledCount() >= 2, 60000); // the reseeded turn

    // Re-arm the fresh session: its own context must cross the threshold.
    pi.send({ id: "3", type: "prompt", message: BIG_PROMPT });
    await pi.waitFor(() => settledCount() >= 3, 60000);

    // The run under test: pi's own bash tool marks T-01 done mid-run - the
    // task-done seam - and the script then chains bash turns without end.
    pi.send({ id: "4", type: "prompt", message: "work the next task" });

    // The fixed behavior: the seam ends the run, the parked fire lands, the
    // session is replaced. On unfixed code the run sails on and this times out.
    const s2 = await waitForSessionChange(pi, s1, "fire");
    assert.notEqual(s2, s1, "the fire must replace the session");

    // The payload that opens the new session is the fresh pass report,
    // generated after the seam: T-01 is done, so the brief steers to T-02 -
    // where the anchoring pass's payload still named T-01 first.
    await pi
      .waitFor(
        () =>
          provider.requests.some((body) => {
            const text = promptText(body);
            return (
              text.includes(BOOTSTRAP_MARKER) &&
              text.includes("Work the next task: T-02, T-03") &&
              !text.includes("T-01, T-02, T-03")
            );
          }),
        60000,
      )
      .catch(() => {
        throw new Error("the post-seam auto payload never reached model context");
      });

    // Unattended means unattended: zero extension_ui_request frames of any
    // method across the whole run - the anchor, the seam and the fire alike
    // (REQ-05: nothing of the extension's reaches the model or the UI here).
    assert.deepEqual(pi.uiRequests(), []);

    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
      `extension errors; stderr: ${pi.stderr()}`,
    );
    assert.deepEqual(pi.unparsed, [], "every frame must parse as JSON");
  });
});
