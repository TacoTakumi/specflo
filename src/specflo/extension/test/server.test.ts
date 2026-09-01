/**
 * Control server core under the fake harness (T-02, REQ-02 / REQ-03).
 *
 * session_start binds a per-session Unix socket in the v1 agent state layout
 * (<base>/<name>/sock) and writes a discovery-capable status.json;
 * session_shutdown removes both. The opt-out (SPECFLO_AGENT_SERVE=0|off, or a
 * serve.off marker in the state base dir) leaves the filesystem untouched. A
 * managed handshake name is used verbatim; a derived identity falls back to
 * the session name or the cwd basename, with a pid suffix on collision.
 */

import { strict as assert } from "node:assert";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";

import { registerControl } from "../src/control/mod.ts";
import { deriveIdentity } from "../src/control/identity.ts";
import { createControlHarness, type ControlHarness } from "./harness.ts";

/** Env keys the module reads; saved and restored around every test. */
const ENV_KEYS = [
  "SPECFLO_AGENT_STATE_DIR",
  "SPECFLO_AGENT_SERVE",
  "SPECFLO_AGENT_NAME",
  "SPECFLO_AGENT_MANAGED",
];

let saved: Record<string, string | undefined>;
let base: string;
let cwd: string;

beforeEach(() => {
  saved = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
  for (const key of ENV_KEYS) delete process.env[key];
  base = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-state-"));
  cwd = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-cwd-"));
  process.env.SPECFLO_AGENT_STATE_DIR = base;
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  fs.rmSync(base, { recursive: true, force: true });
  fs.rmSync(cwd, { recursive: true, force: true });
});

function harnessInCwd(): ControlHarness {
  const harness = createControlHarness({ cwd });
  registerControl(harness.api);
  return harness;
}

/** The one record directory under ``base``, asserted to be exactly one. */
function onlyRecordDir(): string {
  const entries = fs.readdirSync(base).filter((name) => name !== "serve.off");
  assert.equal(entries.length, 1, `expected one record dir, saw: ${entries.join(", ")}`);
  return path.join(base, entries[0]);
}

function readStatus(dir: string): any {
  return JSON.parse(fs.readFileSync(path.join(dir, "status.json"), "utf8"));
}

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

describe("control server core (T-02)", () => {
  test("session_start binds a connectable socket and writes the discovery record", async () => {
    const harness = harnessInCwd();
    await harness.startSession();
    try {
      const dir = onlyRecordDir();
      assert.ok(await connectable(path.join(dir, "sock")), "socket must accept a connection");
      const status = readStatus(dir);
      assert.equal(status.transport, "tui");
      assert.equal(status.ownership, "adopted");
      assert.equal(status.pid, process.pid);
      assert.equal(status.socket, path.join(dir, "sock"));
      assert.equal(status.cwd, cwd);
      assert.equal(status.name, path.basename(dir));
      assert.equal(path.basename(dir), path.basename(cwd));
    } finally {
      await harness.shutdownSession();
    }
  });

  test("session_shutdown removes the socket and the record", async () => {
    const harness = harnessInCwd();
    await harness.startSession();
    const dir = onlyRecordDir();
    await harness.shutdownSession();
    assert.equal(fs.existsSync(path.join(dir, "sock")), false);
    assert.equal(fs.existsSync(path.join(dir, "status.json")), false);
    assert.equal(await connectable(path.join(dir, "sock")), false);
  });

  for (const value of ["0", "off"]) {
    test(`SPECFLO_AGENT_SERVE=${value} binds nothing and writes nothing`, async () => {
      process.env.SPECFLO_AGENT_SERVE = value;
      const harness = harnessInCwd();
      await harness.startSession();
      await harness.shutdownSession();
      assert.deepEqual(fs.readdirSync(base), []);
    });
  }

  test("a serve.off marker in the state base dir disables serving", async () => {
    fs.writeFileSync(path.join(base, "serve.off"), "");
    const harness = harnessInCwd();
    await harness.startSession();
    await harness.shutdownSession();
    assert.deepEqual(fs.readdirSync(base), ["serve.off"]);
  });

  test("the handshake name is used verbatim and marks the session managed", async () => {
    process.env.SPECFLO_AGENT_NAME = "alpha-7";
    process.env.SPECFLO_AGENT_MANAGED = "1";
    const harness = harnessInCwd();
    await harness.startSession();
    try {
      const dir = onlyRecordDir();
      assert.equal(path.basename(dir), "alpha-7");
      const status = readStatus(dir);
      assert.equal(status.name, "alpha-7");
      assert.equal(status.ownership, "managed");
    } finally {
      await harness.shutdownSession();
    }
  });

  test("a derived name already claimed gets a pid suffix", async () => {
    // Another session claimed the cwd-basename identity: its record exists.
    const claimed = path.join(base, path.basename(cwd));
    fs.mkdirSync(claimed, { recursive: true });
    fs.writeFileSync(path.join(claimed, "status.json"), JSON.stringify({ pid: 1 }));

    const harness = harnessInCwd();
    await harness.startSession();
    try {
      const expected = `${path.basename(cwd)}-${process.pid}`;
      const dir = path.join(base, expected);
      assert.ok(fs.existsSync(path.join(dir, "status.json")), "suffixed record must exist");
      assert.equal(readStatus(dir).name, expected);
    } finally {
      await harness.shutdownSession();
    }
  });
});

describe("identity derivation (T-02)", () => {
  test("prefers the handshake, then the session name, then the cwd basename", () => {
    const claims = () => false;
    assert.deepEqual(
      deriveIdentity({
        env: { SPECFLO_AGENT_NAME: "named", SPECFLO_AGENT_MANAGED: "1" },
        sessionName: "sess",
        cwd: "/tmp/proj",
        pid: 42,
        claimed: claims,
      }),
      { name: "named", ownership: "managed" },
    );
    assert.deepEqual(
      deriveIdentity({ env: {}, sessionName: "sess", cwd: "/tmp/proj", pid: 42, claimed: claims }),
      { name: "sess", ownership: "adopted" },
    );
    assert.deepEqual(
      deriveIdentity({ env: {}, cwd: "/tmp/proj", pid: 42, claimed: claims }),
      { name: "proj", ownership: "adopted" },
    );
  });

  test("sanitizes a derived name to the v1 agent-name alphabet", () => {
    const identity = deriveIdentity({
      env: {},
      sessionName: "my session! (2)",
      cwd: "/tmp/x",
      pid: 42,
      claimed: () => false,
    });
    assert.match(identity.name, /^[A-Za-z0-9][A-Za-z0-9._-]*$/);
  });

  test("suffixes the pid only when the candidate is claimed", () => {
    assert.equal(
      deriveIdentity({ env: {}, cwd: "/tmp/proj", pid: 42, claimed: (name) => name === "proj" })
        .name,
      "proj-42",
    );
  });
});
