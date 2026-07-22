/**
 * The installed extension loads in a real pi and gets in nobody's way (T-15).
 *
 * These drive an actual `pi --mode rpc` process over JSONL, against a stub
 * model provider so the run is hermetic and the "model" says exactly what each
 * case needs. They cover REQ-23 (the installed layout is what pi discovers) and
 * REQ-03 (the extension gates nothing - a tool call a specflo HARD-GATE forbids
 * still executes, because blocking it was never the extension's job).
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { after, describe, it } from "node:test";

import {
  createWorkspace,
  piArgs,
  spawnPi,
  splitJsonlLines,
  startStubProvider,
  type StubProvider,
  type Workspace,
} from "./rpc-harness.ts";

const TIMEOUT = 60000;

describe("strict JSONL framing", () => {
  it("splits on newline only and strips a trailing carriage return", () => {
    const payload = JSON.stringify({ type: "x", text: "a b c" });
    const { lines, rest } = splitJsonlLines(`${payload}\r\n${payload}\npartial`);

    assert.deepEqual(lines, [payload, payload]);
    assert.equal(rest, "partial");
    // U+2028/U+2029 are legal inside a JSON string and must not frame a record.
    assert.equal(JSON.parse(lines[0]).text, "a b c");
  });

  it("holds an incomplete record until its newline arrives", () => {
    const first = splitJsonlLines('{"type":"a"}\n{"type":');
    assert.deepEqual(first.lines, ['{"type":"a"}']);
    const second = splitJsonlLines(`${first.rest}"b"}\n`);
    assert.deepEqual(second.lines, ['{"type":"b"}']);
  });
});

describe("harness contract", () => {
  it("never invokes pi in one-shot print mode", () => {
    const args = piArgs(["--session-dir", "/tmp/x"]);
    assert.ok(args.includes("--mode") && args[args.indexOf("--mode") + 1] === "rpc");
    assert.ok(!args.includes("-p"), "harness must not use pi -p");
    assert.ok(!args.includes("--print"), "harness must not use pi --print");
  });
});

describe("a real pi session with the extension installed", () => {
  const cleanups: Array<() => void | Promise<void>> = [];
  after(async () => {
    for (const cleanup of cleanups.reverse()) await cleanup();
  });

  const workspaceFor = async (turns: Parameters<typeof startStubProvider>[0]) => {
    const provider: StubProvider = await startStubProvider(turns);
    // Registered before anything else can throw: a listening server left open
    // by a failed set-up keeps the whole test process alive.
    cleanups.push(() => provider.close());
    const workspace: Workspace = createWorkspace(provider);
    cleanups.push(() => workspace.cleanup());
    return { provider, workspace };
  };

  it(
    "installs where pi discovers it, with no settings entry",
    { timeout: TIMEOUT },
    async () => {
      const { workspace } = await workspaceFor([{ text: "hi" }]);

      assert.ok(fs.existsSync(path.join(workspace.extensionDir, "package.json")));
      assert.ok(fs.existsSync(path.join(workspace.extensionDir, "src", "index.ts")));
      const settings = JSON.parse(
        fs.readFileSync(path.join(workspace.agentDir, "settings.json"), "utf8"),
      );
      assert.deepEqual(settings.packages, [], "install must not register a packages entry");
    },
  );

  it("reaches agent_settled with zero extension errors", { timeout: TIMEOUT }, async () => {
    const { workspace } = await workspaceFor([{ text: "hello from the stub" }]);
    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());

    pi.send({ id: "1", type: "prompt", message: "say hi" });
    await pi.waitFor((event) => event.type === "agent_settled", TIMEOUT - 5000);

    assert.deepEqual(
      pi.events.filter((event) => event.type === "extension_error"),
      [],
      `extension errors: ${pi.stderr()}`,
    );
    assert.deepEqual(pi.unparsed, [], "every frame must parse as JSON");
  });

  it("fails loudly when the extension it discovers is broken", { timeout: TIMEOUT }, async () => {
    // The positive control for the assertion above: zero extension errors is
    // only meaningful if pi is looking at this directory at all. Break the
    // installed copy and pi must refuse to start.
    const { workspace } = await workspaceFor([{ text: "unused" }]);
    fs.writeFileSync(
      path.join(workspace.extensionDir, "src", "index.ts"),
      "this is not valid typescript (((\n",
    );

    const pi = spawnPi(workspace);
    cleanups.push(() => pi.close());
    const code = await pi.exited();

    assert.notEqual(code, 0, "pi should exit non-zero on a broken discovered extension");
    assert.match(pi.stderr(), /Failed to load extension/);
    assert.match(pi.stderr(), /extensions[/\\]specflo/);
  });

  it(
    "leaves a tool call a specflo HARD-GATE forbids unblocked",
    { timeout: TIMEOUT },
    async () => {
      // Editing the plan in place instead of superseding a task is exactly what
      // the execute HARD-GATE forbids. The extension registers no tool_call
      // handler, so the call runs: specflo's gates are the skill's to enforce,
      // never the extension's (REQ-03).
      const { workspace } = await workspaceFor([
        { toolCall: { name: "bash", arguments: { command: "echo 'rewritten in place' >> plan.md" } } },
        { text: "done" },
      ]);
      const planPath = path.join(workspace.project, "plan.md");
      fs.writeFileSync(planPath, "# plan\n");

      const pi = spawnPi(workspace);
      cleanups.push(() => pi.close());
      pi.send({ id: "1", type: "prompt", message: "rewrite the plan" });
      await pi.waitFor((event) => event.type === "agent_settled", TIMEOUT - 5000);

      const toolEvents = pi.events.filter((event) => String(event.type).startsWith("tool_execution"));
      assert.ok(toolEvents.some((event) => event.type === "tool_execution_start"));
      assert.ok(toolEvents.some((event) => event.type === "tool_execution_end"));
      assert.match(fs.readFileSync(planPath, "utf8"), /rewritten in place/);
      assert.deepEqual(
        pi.events.filter((event) => event.type === "extension_error"),
        [],
      );
    },
  );
});
