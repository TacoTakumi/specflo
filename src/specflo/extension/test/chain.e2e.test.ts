/**
 * The chain, end to end: two consecutive extension-driven fires in one real pi
 * run, with a single typed command at the very start (T-04, REQ-01 / REQ-03).
 *
 * auto-fire.e2e.test.ts proves one unattended fire. This proves the *second*
 * one - the whole of REQ-01. What breaks it is the shape a real auto run has:
 * the next seam is declared while the previous fire's own reseed run is still
 * going. On pre-fix code `reseedInto` awaits the reseed's `sendMessage`, which
 * pi settles only when the run that message started ends, so the fire's
 * `finally` never runs and `fireInFlight` is still held when the seam arrives.
 * The seam is swallowed, the chain dies after one fire, and the run - scripted
 * to chain tool calls without end, so only an abort can stop it - sails on
 * until this test times out waiting for the second session change.
 *
 * Everything is real, as in auto-fire.e2e.test.ts and sail-on.e2e.test.ts: the
 * project is driven to the execute phase through the actual CLI gates, the auto
 * run is started by the CLI before pi spawns, the anchor is the
 * ReplacedSessionContext captured at the user-typed `/specflo-continue auto`,
 * and each seam is a task genuinely reaching done through the real CLI - the
 * second one run by pi's own bash tool, mid-run, inside the window the bug
 * lives in. That call also prints 30k characters, so the tool result carries
 * the replacement session past the arming threshold on the same turn: pi
 * reports usage including the tool result at the turn_end that follows it.
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

const TIMEOUT = 240000;

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
 *
 * The execute phase is where a task-done seam exists at all, and three tasks
 * give the chain two seams to cross with one still pending afterwards - so the
 * run never ends for a reason other than the ones under test.
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

describe("two consecutive extension-driven fires in one real pi run", () => {
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
   * A clear crosses waitForIdle, a subprocess and a session switch, so there is
   * no single event to wait on; the session change itself is the observable
   * outcome. On pre-fix code the second call is where this test dies - the
   * second seam was swallowed by the held latch, so no fire was ever parked
   * and the scripted run never stops on its own.
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

  it("fires at every seam, not only the first", { timeout: TIMEOUT }, async () => {
    // The scripted model, in the order the stub serves it:
    //   [0] the anchoring clear's reseed turn
    //   [1] the arming turn in the anchored session
    //   [2] the turn whose turn_end declares seam one
    //   [3] the first fire's reseed run: one bash call that marks T-02 done
    //       through the real CLI - seam two, made inside the run the previous
    //       fire started - and prints 30k characters, so the turn_end that
    //       follows is armed on the tool result alone.
    //   [4] every turn after: another bash call, forever. A run the seam does
    //       not end never goes idle, so a swallowed seam is a timeout, not a
    //       quietly passing test.
    const provider: StubProvider = await startStubProvider([
      { text: "ok" },
      { text: "ok" },
      { text: "ok" },
      {
        toolCall: {
          name: "bash",
          arguments: {
            command:
              "specflo task start T-02 && specflo task done T-02 && " +
              "head -c 30000 /dev/zero | tr '\\0' x",
          },
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
      dir: string;
      phase: string;
      auto_run: { under_way: boolean };
    };
    assert.equal(statusJson.phase, "execute", "the project must reach the execute phase");
    assert.equal(statusJson.auto_run.under_way, true, "the run must be under way before pi spawns");

    /** The run's durable pass counter - the CLI's own record of how often it ran. */
    const passes = (): number => {
      const file = path.join(statusJson.dir, "auto-run.json");
      return (JSON.parse(fs.readFileSync(file, "utf8")) as { passes?: number }).passes ?? 0;
    };
    assert.equal(passes(), 1, "the CLI's own first pass");

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    const settledCount = () =>
      pi.events.filter((event) => event.type === "agent_settled").length;

    const s0 = await sessionId(pi, "s0");

    // The anchoring opt-in, exactly as a user types it - and the only thing
    // typed in this whole test. Every replacement after it is the extension's.
    pi.send({ id: "1", type: "prompt", message: "/specflo-continue auto" });
    const s1 = await waitForSessionChange(pi, s0, "anchor");
    await pi.waitFor(() => settledCount() >= 1, 60000); // the reseeded turn
    const passesAfterAnchor = passes();
    assert.equal(passesAfterAnchor, 2, "the anchoring clear ran one pass of its own");

    // Arm the anchored session: its own context must cross the threshold.
    pi.send({ id: "2", type: "prompt", message: BIG_PROMPT });
    await pi.waitFor(() => settledCount() >= 2, 60000);

    // Seam one, made of real state: T-01 reaches done through the real CLI,
    // after the anchored session read its own baseline.
    runSpecflo(workspace, ["task", "start", "T-01"]);
    runSpecflo(workspace, ["task", "done", "T-01"]);

    // A turn whose turn_end declares seam one. The armed anchored fire
    // replaces the session with no input of any kind.
    pi.send({ id: "3", type: "prompt", message: "hi" });
    const s2 = await waitForSessionChange(pi, s1, "fire-one");
    assert.notEqual(s2, s1, "the first fire must replace the session");
    assert.equal(passes(), passesAfterAnchor + 1, "the first fire ran exactly one pass");

    // The first fire's payload: generated after T-01 went done, so it steers
    // to T-02 where the anchoring pass's payload still named T-01.
    await pi
      .waitFor(
        () =>
          provider.requests.some((body) => {
            const text = promptText(body);
            return text.includes(BOOTSTRAP_MARKER) && text.includes("T-02, T-03");
          }),
        60000,
      )
      .catch(() => {
        throw new Error("the first fire's auto payload never reached model context");
      });

    // Seam two needs nothing from here: the reseed run the first fire started
    // is marking T-02 done through pi's own bash tool, and the turn_end after
    // that tool result is armed, anchored and mid-run. Pre-fix, the latch that
    // fire still holds swallows it and the scripted run sails on until this
    // times out. REQ-01 is exactly that it does not.
    const s3 = await waitForSessionChange(pi, s2, "fire-two");
    assert.notEqual(s3, s2, "the second fire must replace the session again");
    assert.notEqual(s3, s1, "and it is a session of its own");
    assert.equal(passes(), passesAfterAnchor + 2, "the second fire ran exactly one pass too");

    // The second fire's payload is its own pass's: T-02 is done now, so only
    // T-03 remains - proof this is a fresh report and not the first one again.
    await pi
      .waitFor(
        () =>
          provider.requests.some((body) => {
            const text = promptText(body);
            return (
              text.includes(BOOTSTRAP_MARKER) &&
              text.includes("T-03") &&
              !text.includes("T-02, T-03")
            );
          }),
        60000,
      )
      .catch(() => {
        throw new Error("the second fire's auto payload never reached model context");
      });

    // Unattended means unattended, across both fires: zero extension_ui_request
    // frames of any method - no notify, no confirm, no select, no input.
    assert.deepEqual(pi.uiRequests(), []);

    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
      `extension errors; stderr: ${pi.stderr()}`,
    );
    assert.deepEqual(pi.unparsed, [], "every frame must parse as JSON");
  });
});
