/**
 * Reload safety and stale-socket handling (T-05, REQ-15 / REQ-18 / REQ-16).
 *
 * shutdown -> start (a /reload) leaves exactly one connectable socket and one
 * record; teardown is idempotent (a double shutdown is a no-op); the event
 * log notes the transitions. On bind, a leftover socket is probed and
 * replaced only when dead - a live listener is never stolen (connect-or-
 * cleanup lifted from remote_pi, MIT, Jacob Moura); a record whose pid is
 * dead is overwritten by a new session with the same identity.
 */

import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { registerControl } from "../src/control/mod.ts";
import { createControlHarness, type ControlHarness } from "./harness.ts";

let savedEnv: Record<string, string | undefined>;
let base: string;
let cwd: string;

const ENV_KEYS = ["SPECFLO_AGENT_STATE_DIR", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"];

function connectable(socketPath: string): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect(socketPath);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
  });
}

/** A pid guaranteed dead: a child that has already exited. */
function deadPid(): number {
  const child = spawnSync("true");
  assert.ok(child.pid);
  return child.pid;
}

function readEvents(dir: string): any[] {
  return fs
    .readFileSync(path.join(dir, "events.jsonl"), "utf8")
    .split("\n")
    .filter((line) => line !== "")
    .map((line) => JSON.parse(line));
}

describe("reload safety and stale sockets (T-05)", () => {
  beforeEach(() => {
    savedEnv = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
    for (const key of ENV_KEYS) delete process.env[key];
    base = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-reload-"));
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-rcwd-"));
    process.env.SPECFLO_AGENT_STATE_DIR = base;
  });

  afterEach(() => {
    for (const key of ENV_KEYS) {
      if (savedEnv[key] === undefined) delete process.env[key];
      else process.env[key] = savedEnv[key];
    }
    fs.rmSync(base, { recursive: true, force: true });
    fs.rmSync(cwd, { recursive: true, force: true });
  });

  function fresh(): ControlHarness {
    const harness = createControlHarness({ cwd });
    registerControl(harness.api);
    return harness;
  }

  function recordDirs(): string[] {
    return fs.readdirSync(base).filter((name) => name !== "serve.off");
  }

  test("shutdown then start leaves exactly one live server and one record", async () => {
    const first = fresh();
    await first.startSession();
    await first.shutdownSession("reload");
    // A /reload rebuilds the extension closure; the replacement session
    // starts its own server for the same identity.
    const second = fresh();
    await second.startSession("reload");
    try {
      assert.deepEqual(recordDirs(), [path.basename(cwd)]);
      const dir = path.join(base, path.basename(cwd));
      assert.ok(await connectable(path.join(dir, "sock")));
      assert.ok(fs.existsSync(path.join(dir, "status.json")));
      const types = readEvents(dir).map((event) => event.type);
      assert.ok(types.includes("control_stop"), `no control_stop in ${types}`);
      assert.ok(types.includes("control_start"), `no control_start in ${types}`);
    } finally {
      await second.shutdownSession();
    }
  });

  test("a double shutdown is a no-op", async () => {
    const harness = fresh();
    await harness.startSession();
    await harness.shutdownSession();
    await harness.shutdownSession();
    const dir = path.join(base, path.basename(cwd));
    assert.equal(fs.existsSync(path.join(dir, "sock")), false);
    assert.equal(fs.existsSync(path.join(dir, "status.json")), false);
    // Exactly one stop transition: the second shutdown had nothing to stop.
    const stops = readEvents(dir).filter((event) => event.type === "control_stop");
    assert.equal(stops.length, 1);
  });

  test("a second start on one closure replaces its own server, not duplicates it", async () => {
    const harness = fresh();
    await harness.startSession();
    await harness.startSession("resume");
    try {
      assert.deepEqual(recordDirs(), [path.basename(cwd)]);
      assert.ok(await connectable(path.join(base, path.basename(cwd), "sock")));
    } finally {
      await harness.shutdownSession();
    }
  });

  test("bind over a dead leftover socket succeeds (REQ-18)", async () => {
    // An unclean death leaves the socket file and a record with a dead pid.
    const dir = path.join(base, path.basename(cwd));
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "sock"), ""); // plain file: connect fails
    fs.writeFileSync(
      path.join(dir, "status.json"),
      JSON.stringify({ name: path.basename(cwd), state: "working", pid: deadPid() }),
    );
    const harness = fresh();
    await harness.startSession();
    try {
      // Same identity, no pid suffix: the dead record was overwritten.
      assert.deepEqual(recordDirs(), [path.basename(cwd)]);
      assert.ok(await connectable(path.join(dir, "sock")));
      const status = JSON.parse(fs.readFileSync(path.join(dir, "status.json"), "utf8"));
      assert.equal(status.pid, process.pid);
      assert.equal(status.state, "idle");
    } finally {
      await harness.shutdownSession();
    }
  });

  test("a live listener is never stolen", async () => {
    // A managed name pins the identity, so the conflict cannot be suffixed
    // away: the planted live server must survive and serving must stand down.
    const dir = path.join(base, "pinned");
    fs.mkdirSync(dir, { recursive: true });
    const planted = net.createServer();
    await new Promise<void>((resolve, reject) => {
      planted.once("error", reject);
      planted.listen(path.join(dir, "sock"), resolve);
    });
    const plantedRecord = { name: "pinned", state: "idle", pid: process.pid };
    fs.writeFileSync(path.join(dir, "status.json"), JSON.stringify(plantedRecord));
    process.env.SPECFLO_AGENT_NAME = "pinned";
    process.env.SPECFLO_AGENT_MANAGED = "1";
    const harness = fresh();
    try {
      await harness.startSession();
      // The planted listener still owns the socket and the record is intact.
      assert.ok(await connectable(path.join(dir, "sock")));
      assert.deepEqual(
        JSON.parse(fs.readFileSync(path.join(dir, "status.json"), "utf8")),
        plantedRecord,
      );
      await harness.shutdownSession();
      // Standing down also means not tearing down what it never owned.
      assert.ok(fs.existsSync(path.join(dir, "sock")));
      assert.ok(fs.existsSync(path.join(dir, "status.json")));
    } finally {
      await new Promise((resolve) => planted.close(resolve));
    }
  });

  test("the lifted connect-or-cleanup carries the remote_pi MIT attribution (REQ-16)", () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const source = fs.readFileSync(path.join(here, "..", "src", "control", "stale.ts"), "utf8");
    assert.match(source, /remote_pi/);
    assert.match(source, /Jacob Moura/);
    assert.match(source, /MIT/);
  });
});
