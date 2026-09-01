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
import { translateCommand, type CommandHost } from "./commands.ts";
import type { Ownership } from "./identity.ts";
import { probeSocket } from "./stale.ts";
import { appendEvent, nowIso, writeStatusFile } from "./statefiles.ts";

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

/**
 * What command translation needs from the live pi session. mod.ts builds one
 * from the extension API and the session_start context; tests fake it.
 */
export interface SessionBridge {
  isIdle(): boolean;
  abort(): void;
  sendUserMessage(content: unknown, options?: { deliverAs: "steer" | "followUp" }): void;
  state(): Record<string, unknown>;
  /**
   * The last final assistant text from the session's own state, or
   * undefined when it cannot be read; survives extension reloads the way
   * pi's getLastAssistantText does.
   */
  lastAssistantText(): string | undefined;
  /** Stand down durably: forget this server and stop it (the detach verb). */
  detach(): void;
}

export interface ControlServerOptions {
  baseDir: string;
  name: string;
  ownership: Ownership;
  cwd: string;
  pid: number;
  /** The herdr pane from the managed handshake, when one was given. */
  herdrPane?: string;
  bridge: SessionBridge;
}

export class ControlServer implements CommandHost {
  readonly root: string;
  readonly socketPath: string;
  readonly statusPath: string;
  readonly eventsPath: string;

  private readonly options: ControlServerOptions;
  private server: net.Server | null = null;
  private readonly connections = new Set<net.Socket>();
  /** pi getLastAssistantText semantics, fed by the mirrored message stream. */
  private lastAssistant: string | undefined;
  /** The last lifecycle state written, so a prompt close can restore it. */
  lifecycle = "idle";

  constructor(options: ControlServerOptions) {
    this.options = options;
    this.root = path.join(options.baseDir, options.name);
    this.socketPath = path.join(this.root, "sock");
    this.statusPath = path.join(this.root, "status.json");
    this.eventsPath = path.join(this.root, "events.jsonl");
  }

  /** Bind the socket and write the discovery record. */
  async start(reason?: string): Promise<void> {
    fs.mkdirSync(this.root, { recursive: true });
    // The event log exists from the moment the session serves, as it does
    // the moment a v1 host starts: `agent log` on an idle agent prints an
    // empty log, not an unknown-agent error. Append mode, so a session
    // rejoining an existing identity extends the log rather than wiping it.
    fs.appendFileSync(this.eventsPath, "");
    // A leftover socket file from an unclean death would fail the bind. The
    // guard is connect-or-cleanup, lifted from remote_pi's single-instance
    // bind (MIT, Jacob Moura; see stale.ts): probe first, and only a socket
    // nothing answers on is unlinked - a live listener is never stolen,
    // because stealing it would orphan the session that owns it (REQ-18).
    if (fs.existsSync(this.socketPath)) {
      if (await probeSocket(this.socketPath)) {
        throw new Error(`a live control server already owns ${this.socketPath}`);
      }
      fs.rmSync(this.socketPath, { force: true });
    }
    const server = net.createServer((socket) => {
      // unref'd like the listener: an open control connection is no reason
      // for pi to stay alive either.
      socket.unref();
      this.connections.add(socket);
      socket.on("close", () => this.connections.delete(socket));
      socket.on("error", () => socket.destroy());
      // LF-JSONL in: buffer to newlines, one command per line (REQ-09).
      socket.setEncoding("utf8");
      let buffer = "";
      socket.on("data", (chunk: string) => {
        buffer += chunk;
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (line.trim() !== "") this.handleLine(socket, line);
        }
      });
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
    // The transition lands in the log (REQ-15): a reader of events.jsonl can
    // see each server generation come up and go down across reloads.
    this.publish(reason === undefined ? { type: "control_start" } : { type: "control_start", reason });
  }

  /** Close the socket and remove both it and the record (REQ-02). */
  async stop(reason?: string): Promise<void> {
    const server = this.server;
    this.server = null;
    if (server !== null) {
      this.publish(reason === undefined ? { type: "control_stop" } : { type: "control_stop", reason });
      for (const socket of this.connections) socket.destroy();
      this.connections.clear();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
    fs.rmSync(this.socketPath, { force: true });
    fs.rmSync(this.statusPath, { force: true });
  }

  /**
   * Publish one RPC-shaped frame the way v1's pump publishes what pi emits:
   * into events.jsonl (ts-stamped) and to every connected client (ts-free,
   * the wire's own shape). Mirrored run events and translated-command
   * responses both come through here (REQ-07, REQ-09).
   */
  publish(frame: Record<string, unknown>): void {
    this.trackLastAssistant(frame);
    appendEvent(this.eventsPath, frame);
    const data = `${JSON.stringify(frame)}\n`;
    for (const socket of this.connections) {
      try {
        socket.write(data);
      } catch {
        socket.destroy();
      }
    }
  }

  /** Drive the v1 lifecycle in status.json: working at start, idle at settle. */
  setLifecycle(state: "working" | "idle"): void {
    this.writeStatus(state);
  }

  /** One socket line: parse, translate, answer or publish. */
  private handleLine(socket: net.Socket, line: string): void {
    let request: unknown;
    try {
      request = JSON.parse(line);
    } catch (exc) {
      this.sendTo(socket, {
        type: "response",
        command: "parse",
        success: false,
        error: `failed to parse command: ${exc instanceof Error ? exc.message : exc}`,
      });
      return;
    }
    try {
      const { response, publish } = translateCommand(this, request);
      if (publish) this.publish(response);
      else this.sendTo(socket, response);
    } catch (exc) {
      // A throwing translation must not kill the connection loop.
      this.sendTo(socket, {
        type: "response",
        command: "error",
        success: false,
        error: `${exc instanceof Error ? exc.message : exc}`,
      });
    }
  }

  private sendTo(socket: net.Socket, frame: Record<string, unknown>): void {
    try {
      socket.write(`${JSON.stringify(frame)}\n`);
    } catch {
      socket.destroy();
    }
  }

  /**
   * Track the last final assistant text off the mirrored message stream,
   * with pi's getLastAssistantText semantics: the newest assistant message
   * that is not an empty abort, its text blocks concatenated.
   */
  private trackLastAssistant(frame: Record<string, unknown>): void {
    if (frame.type !== "message_end") return;
    const message = frame.message as
      | { role?: unknown; stopReason?: unknown; content?: unknown }
      | undefined;
    if (message?.role !== "assistant" || !Array.isArray(message.content)) return;
    if (message.stopReason === "aborted" && message.content.length === 0) return;
    let text = "";
    for (const block of message.content) {
      if (block !== null && typeof block === "object" && (block as any).type === "text") {
        text += (block as any).text ?? "";
      }
    }
    this.lastAssistant = text;
  }

  // --- CommandHost ----------------------------------------------------------

  isIdle(): boolean {
    return this.options.bridge.isIdle();
  }

  abort(): void {
    this.options.bridge.abort();
  }

  sendUserMessage(content: unknown, options?: { deliverAs: "steer" | "followUp" }): void {
    this.options.bridge.sendUserMessage(content, options);
  }

  /** The v1 `status` host-verb payload, read from disk exactly as v1 reads it. */
  statusData(): unknown {
    return {
      status: JSON.parse(fs.readFileSync(this.statusPath, "utf8")),
      paths: {
        socket: this.socketPath,
        events: this.eventsPath,
        status: this.statusPath,
      },
    };
  }

  state(): Record<string, unknown> {
    return this.options.bridge.state();
  }

  lastAssistantText(): string | undefined {
    // Session state is the truth - it survives reloads; the mirror-tracked
    // value covers a session manager that cannot be read.
    return this.options.bridge.lastAssistantText() ?? this.lastAssistant;
  }

  requestDetach(): void {
    this.options.bridge.detach();
  }

  /** Atomically replace status.json, the same tmp-then-rename dance as v1. */
  writeStatus(state: string): void {
    this.lifecycle = state;
    const snapshot = {
      // v1 core fields, so the v1 reader parses this record unchanged.
      name: this.options.name,
      state,
      host_pid: this.options.pid,
      pi_pid: this.options.pid,
      context_percent: null,
      herdr_workspace: null,
      herdr_tab: null,
      // Recorded so an ownership-aware stop can release the registration
      // even when this session dies without its own shutdown (REQ-08).
      herdr_pane: this.options.herdrPane ?? null,
      last_activity: nowIso(),
      // Discovery fields this project adds (REQ-02).
      transport: "tui",
      ownership: this.options.ownership,
      pid: this.options.pid,
      socket: this.socketPath,
      cwd: this.options.cwd,
    };
    writeStatusFile(this.statusPath, snapshot);
  }
}
