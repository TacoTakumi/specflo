/**
 * Command translation and broadcast over the control socket (T-04,
 * REQ-09 / REQ-10 / REQ-11).
 *
 * The socket speaks the v1 host contract: LF-JSONL frames in, pi-shaped
 * response frames out ({type:"response", command, id?, success, data|error}),
 * the host verb `status` answered locally to the requester, translated
 * commands' responses and every mirrored run event broadcast to all
 * connected clients, a plain prompt refused while streaming, and an
 * unsupported type rejected by name with the connection staying usable.
 */

import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";

import { registerControl } from "../src/control/mod.ts";
import { createControlHarness, type ControlHarness } from "./harness.ts";

/** A tiny LF-JSONL socket client: collects frames, waits on predicates. */
class Client {
  frames: any[] = [];
  private socket: net.Socket;
  private buffer = "";
  private watchers = new Set<() => void>();

  constructor(socketPath: string) {
    this.socket = net.connect(socketPath);
    this.socket.setEncoding("utf8");
    this.socket.on("data", (chunk: string) => {
      this.buffer += chunk;
      const lines = this.buffer.split("\n");
      this.buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line !== "") this.frames.push(JSON.parse(line));
      }
      for (const notify of this.watchers) notify();
    });
  }

  connected(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.socket.once("connect", resolve);
      this.socket.once("error", reject);
    });
  }

  send(frame: Record<string, unknown>): void {
    this.socket.write(`${JSON.stringify(frame)}\n`);
  }

  sendRaw(text: string): void {
    this.socket.write(text);
  }

  waitFor(predicate: (frame: any) => boolean, timeoutMs = 5000): Promise<any> {
    return new Promise((resolve, reject) => {
      const check = () => {
        const found = this.frames.find(predicate);
        if (found !== undefined) {
          cleanup();
          resolve(found);
        }
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(
          new Error(`no matching frame in ${timeoutMs}ms; saw: ${JSON.stringify(this.frames)}`),
        );
      }, timeoutMs);
      const cleanup = () => {
        clearTimeout(timer);
        this.watchers.delete(check);
      };
      this.watchers.add(check);
      check();
    });
  }

  response(id: string, timeoutMs = 5000): Promise<any> {
    return this.waitFor((frame) => frame.type === "response" && frame.id === id, timeoutMs);
  }

  close(): void {
    this.socket.destroy();
  }
}

let savedStateDir: string | undefined;
let base: string;
let cwd: string;
let clients: Client[];

describe("command translation and broadcast (T-04)", () => {
  beforeEach(() => {
    savedStateDir = process.env.SPECFLO_AGENT_STATE_DIR;
    base = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-cmd-"));
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-ccwd-"));
    process.env.SPECFLO_AGENT_STATE_DIR = base;
    clients = [];
  });

  afterEach(() => {
    for (const client of clients) client.close();
    if (savedStateDir === undefined) delete process.env.SPECFLO_AGENT_STATE_DIR;
    else process.env.SPECFLO_AGENT_STATE_DIR = savedStateDir;
    fs.rmSync(base, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  });

  async function serve(): Promise<{ harness: ControlHarness; socketPath: string }> {
    const harness = createControlHarness({ cwd });
    registerControl(harness.api);
    await harness.startSession();
    return { harness, socketPath: path.join(base, path.basename(cwd), "sock") };
  }

  async function client(socketPath: string): Promise<Client> {
    const c = new Client(socketPath);
    clients.push(c);
    await c.connected();
    return c;
  }

  test("status is answered locally with the v1 data shape", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.send({ type: "status", id: "s1" });
      const response = await c.response("s1");
      assert.equal(response.command, "status");
      assert.equal(response.success, true);
      assert.equal(response.data.status.name, path.basename(cwd));
      assert.equal(response.data.status.state, "idle");
      assert.equal(response.data.paths.socket, socketPath);
      assert.ok(response.data.paths.events.endsWith("events.jsonl"));
      assert.ok(response.data.paths.status.endsWith("status.json"));
    } finally {
      await harness.shutdownSession();
    }
  });

  test("prompt on an idle agent delivers via sendUserMessage and succeeds", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.send({ type: "prompt", id: "p1", message: "run the tests" });
      const response = await c.response("p1");
      assert.equal(response.success, true);
      assert.equal(response.command, "prompt");
      assert.deepEqual(harness.sent, [{ content: "run the tests", options: undefined }]);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("a plain prompt is refused while streaming, and nothing is delivered", async () => {
    const { harness, socketPath } = await serve();
    try {
      harness.idle = false;
      const c = await client(socketPath);
      c.send({ type: "prompt", id: "p1", message: "now" });
      const response = await c.response("p1");
      assert.equal(response.success, false);
      assert.match(response.error, /busy|streaming/i);
      assert.deepEqual(harness.sent, []);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("prompt with streamingBehavior maps to deliverAs and is delivered while streaming", async () => {
    const { harness, socketPath } = await serve();
    try {
      harness.idle = false;
      const c = await client(socketPath);
      c.send({ type: "prompt", id: "p1", message: "steer this", streamingBehavior: "steer" });
      const response = await c.response("p1");
      assert.equal(response.success, true);
      assert.deepEqual(harness.sent, [
        { content: "steer this", options: { deliverAs: "steer" } },
      ]);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("steer and follow_up translate to their deliverAs values", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.send({ type: "steer", id: "s1", message: "left" });
      assert.equal((await c.response("s1")).success, true);
      c.send({ type: "follow_up", id: "f1", message: "then this" });
      assert.equal((await c.response("f1")).success, true);
      assert.deepEqual(harness.sent, [
        { content: "left", options: { deliverAs: "steer" } },
        { content: "then this", options: { deliverAs: "followUp" } },
      ]);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("abort aborts the run; get_state reports streaming from the live session", async () => {
    const { harness, socketPath } = await serve();
    try {
      harness.idle = false;
      const c = await client(socketPath);
      c.send({ type: "abort", id: "a1" });
      assert.equal((await c.response("a1")).success, true);
      assert.equal(harness.abortCalls.length, 1);
      c.send({ type: "get_state", id: "g1" });
      const state = (await c.response("g1")).data;
      assert.equal(state.isStreaming, true);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("get_last_assistant_text returns the text of the last assistant message", async () => {
    const { harness, socketPath } = await serve();
    try {
      await harness.emit({
        type: "message_end",
        message: {
          role: "assistant",
          content: [
            { type: "text", text: "part one; " },
            { type: "thinking", thinking: "hidden" },
            { type: "text", text: "part two" },
          ],
        },
      });
      const c = await client(socketPath);
      c.send({ type: "get_last_assistant_text", id: "l1" });
      const response = await c.response("l1");
      assert.equal(response.success, true);
      assert.equal(response.data.text, "part one; part two");
    } finally {
      await harness.shutdownSession();
    }
  });

  test("responses and mirrored events are broadcast; ids stay correlated per client", async () => {
    const { harness, socketPath } = await serve();
    try {
      const a = await client(socketPath);
      const b = await client(socketPath);
      a.send({ type: "get_state", id: "from-a" });
      b.send({ type: "get_state", id: "from-b" });
      const toA = await a.response("from-a");
      const toB = await b.response("from-b");
      assert.equal(toA.success, true);
      assert.equal(toB.success, true);
      // Translated-command responses are broadcast, as pi's responses are in
      // v1's pumped stream: each client also sees the other's frame.
      await a.waitFor((frame) => frame.type === "response" && frame.id === "from-b");
      await b.waitFor((frame) => frame.type === "response" && frame.id === "from-a");
      // A mirrored run event reaches every connected client, ts-free like
      // v1's wire frames.
      await harness.emit({ type: "agent_start" });
      const eventAtA = await a.waitFor((frame) => frame.type === "agent_start");
      const eventAtB = await b.waitFor((frame) => frame.type === "agent_start");
      assert.equal(eventAtA.ts, undefined);
      assert.equal(eventAtB.ts, undefined);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("an unsupported type is rejected by name and the connection stays usable", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.send({ type: "export_html", id: "x1" });
      const rejection = await c.response("x1");
      assert.equal(rejection.success, false);
      assert.match(rejection.error, /export_html/);
      c.send({ type: "status", id: "s1" });
      assert.equal((await c.response("s1")).success, true);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("an unparseable line gets the v1 parse rejection and the connection stays usable", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.sendRaw("this is not json\n");
      const rejection = await c.waitFor(
        (frame) => frame.type === "response" && frame.command === "parse",
      );
      assert.equal(rejection.success, false);
      c.send({ type: "status", id: "s1" });
      assert.equal((await c.response("s1")).success, true);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("a command with no type gets the parse rejection", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.send({ id: "n1", message: "typeless" });
      const rejection = await c.waitFor(
        (frame) => frame.type === "response" && frame.command === "parse",
      );
      assert.equal(rejection.success, false);
    } finally {
      await harness.shutdownSession();
    }
  });

  test("detach stands the server down durably: later events resurrect nothing", async () => {
    const { harness, socketPath } = await serve();
    const dir = path.join(base, path.basename(cwd));
    const c = await client(socketPath);
    c.send({ type: "detach", id: "d1" });
    const response = await c.response("d1");
    assert.equal(response.success, true);
    // The serving side removes its own registration...
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      if (!fs.existsSync(path.join(dir, "sock")) && !fs.existsSync(path.join(dir, "status.json")))
        break;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    assert.equal(fs.existsSync(path.join(dir, "sock")), false);
    assert.equal(fs.existsSync(path.join(dir, "status.json")), false);
    // ...and stays down: a live session's next lifecycle event must not
    // recreate the record the detach removed (review round 1, finding 1).
    const logSize = fs.statSync(path.join(dir, "events.jsonl")).size;
    await harness.emit({ type: "agent_start" });
    await harness.emit({ type: "agent_settled" });
    assert.equal(fs.existsSync(path.join(dir, "status.json")), false);
    assert.equal(fs.statSync(path.join(dir, "events.jsonl")).size, logSize);
    await harness.shutdownSession(); // idempotent no-op after detach
  });

  test("get_last_assistant_text reads session state, so it survives a reload", async () => {
    // A fresh server generation (post-/reload) has mirrored nothing; the
    // answer comes from the session branch (review round 1, finding 4).
    const harness = createControlHarness({
      cwd,
      branch: [
        { type: "message", message: { role: "user", content: [{ type: "text", text: "q" }] } },
        {
          type: "message",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "remembered across reloads" }],
            stopReason: "stop",
          },
        },
      ],
    });
    registerControl(harness.api);
    await harness.startSession("reload");
    try {
      const c = await client(path.join(base, path.basename(cwd), "sock"));
      c.send({ type: "get_last_assistant_text", id: "l1" });
      const response = await c.response("l1");
      assert.equal(response.data.text, "remembered across reloads");
    } finally {
      await harness.shutdownSession();
    }
  });

  test("translated responses land in events.jsonl like v1's pumped stream", async () => {
    const { harness, socketPath } = await serve();
    try {
      const c = await client(socketPath);
      c.send({ type: "prompt", id: "p1", message: "logged" });
      await c.response("p1");
      const lines = fs
        .readFileSync(path.join(base, path.basename(cwd), "events.jsonl"), "utf8")
        .split("\n")
        .filter((line) => line !== "")
        .map((line) => JSON.parse(line));
      const logged = lines.find((event) => event.type === "response" && event.id === "p1");
      assert.ok(logged, "the response frame must be mirrored into the event log");
      assert.equal(typeof logged.ts, "string");
    } finally {
      await harness.shutdownSession();
    }
  });
});
