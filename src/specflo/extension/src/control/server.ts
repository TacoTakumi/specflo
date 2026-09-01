/**
 * The control server core: one Unix domain socket per served session, bound in
 * the v1 agent state layout, plus the discovery record that makes the session
 * findable (REQ-02).
 *
 *     <base>/<name>/
 *         sock          the control socket (this module binds it)
 *         status.json   discovery-capable snapshot, atomically replaced
 *
 * <base> is $SPECFLO_AGENT_STATE_DIR when set, else ~/.specflo/agents - the
 * same resolution the v1 Python subsystem uses, so both transports share one
 * discovery directory. The opt-out (REQ-03) is SPECFLO_AGENT_SERVE=0|off or a
 * serve.off marker file in <base>; with it set nothing is bound and nothing is
 * written.
 *
 * status.json carries the v1 core fields (name, state, pids, last_activity,
 * the herdr slots) so v1 readers parse it, plus the discovery fields this
 * project adds: transport ("tui"), ownership (managed/adopted), pid, the
 * socket path, and the session cwd.
 */

import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { Ownership } from "./identity.ts";

export const ENV_STATE_DIR = "SPECFLO_AGENT_STATE_DIR";
export const ENV_SERVE = "SPECFLO_AGENT_SERVE";

/** The state base directory, resolved exactly as the v1 subsystem resolves it. */
export function stateBaseDir(env: Record<string, string | undefined> = process.env): string {
  const dir = env[ENV_STATE_DIR];
  return dir ? dir : path.join(os.homedir(), ".specflo", "agents");
}

/** Whether the control surface is switched off entirely (REQ-03). */
export function servingDisabled(
  env: Record<string, string | undefined>,
  baseDir: string,
): boolean {
  const value = (env[ENV_SERVE] ?? "").toLowerCase();
  if (value === "0" || value === "off") return true;
  return fs.existsSync(path.join(baseDir, "serve.off"));
}

/** UTC now in v1's wire format: ISO 8601, millisecond precision, +00:00. */
export function nowIso(): string {
  return new Date().toISOString().replace("Z", "+00:00");
}

export interface ControlServerOptions {
  baseDir: string;
  name: string;
  ownership: Ownership;
  cwd: string;
  pid: number;
}

export class ControlServer {
  readonly root: string;
  readonly socketPath: string;
  readonly statusPath: string;

  private readonly options: ControlServerOptions;
  private server: net.Server | null = null;
  private readonly connections = new Set<net.Socket>();

  constructor(options: ControlServerOptions) {
    this.options = options;
    this.root = path.join(options.baseDir, options.name);
    this.socketPath = path.join(this.root, "sock");
    this.statusPath = path.join(this.root, "status.json");
  }

  /** Bind the socket and write the discovery record. */
  async start(): Promise<void> {
    fs.mkdirSync(this.root, { recursive: true });
    // A leftover socket file from an unclean death would fail the bind; v1's
    // host unlinks before binding and so does this. (T-05 adds the probe that
    // distinguishes a dead leftover from a live server.)
    fs.rmSync(this.socketPath, { force: true });
    const server = net.createServer((socket) => {
      // unref'd like the listener: an open control connection is no reason
      // for pi to stay alive either.
      socket.unref();
      this.connections.add(socket);
      socket.on("close", () => this.connections.delete(socket));
    });
    // The control surface must never hold pi open: an unref'd listener does
    // not count toward the event loop, so a pi that is otherwise done exits
    // and the socket dies with it instead of pinning the process forever.
    server.unref();
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(this.socketPath, resolve);
    });
    this.server = server;
    this.writeStatus("idle");
  }

  /** Close the socket and remove both it and the record (REQ-02). */
  async stop(): Promise<void> {
    const server = this.server;
    this.server = null;
    if (server !== null) {
      for (const socket of this.connections) socket.destroy();
      this.connections.clear();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
    fs.rmSync(this.socketPath, { force: true });
    fs.rmSync(this.statusPath, { force: true });
  }

  /** Atomically replace status.json, the same tmp-then-rename dance as v1. */
  writeStatus(state: string): void {
    const snapshot = {
      // v1 core fields, so the v1 reader parses this record unchanged.
      name: this.options.name,
      state,
      host_pid: this.options.pid,
      pi_pid: this.options.pid,
      context_percent: null,
      herdr_workspace: null,
      herdr_tab: null,
      herdr_pane: null,
      last_activity: nowIso(),
      // Discovery fields this project adds (REQ-02).
      transport: "tui",
      ownership: this.options.ownership,
      pid: this.options.pid,
      socket: this.socketPath,
      cwd: this.options.cwd,
    };
    const tmp = `${this.statusPath}.tmp.${this.options.pid}`;
    fs.writeFileSync(tmp, `${JSON.stringify(snapshot, null, 2)}\n`);
    fs.renameSync(tmp, this.statusPath);
  }
}
