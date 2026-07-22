/**
 * End-to-end harness: drive a real pi over RPC mode and watch what it emits.
 *
 * pi's RPC mode is the sanctioned way to observe an extension in a live agent:
 * strict JSONL on stdin/stdout, persistent sessions, extensions loaded exactly
 * as in an interactive run, and every `ctx.ui.*` call surfaced as an
 * `extension_ui_request` frame. `pi -p` is never used - it is a one-shot with no
 * stdin, so it can observe an output shape and nothing else.
 *
 * The run is hermetic. A stub provider speaking the OpenAI chat-completions
 * wire format stands in for the model, so a suite decides exactly what the
 * "model" says - plain text, or a tool call - and no network, API key or local
 * inference server is involved. Everything pi reads lives in a temp directory:
 * its agent dir (models.json, settings.json, extensions), its session dir, and
 * the project it runs in.
 *
 * Nothing here runs inside pi, so the structural guards that forbid the
 * extension from touching the filesystem or spawning processes do not apply -
 * this is the test rig, and spawning pi is the point.
 */

import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import * as fs from "node:fs";
import * as http from "node:http";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/** The pi binary under test. Overridable for a non-PATH install. */
export const PI_BIN = process.env.SPECFLO_PI_BIN ?? "pi";

/** The specflo binary that installs the extension into the temp agent dir. */
export const SPECFLO_BIN = process.env.SPECFLO_BIN ?? "specflo";

/** Provider and model ids the stub registers; pi is pointed at them by name. */
export const STUB_PROVIDER = "stub";
export const STUB_MODEL = "stub-model";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** The extension package root - the directory this test dir lives in. */
export const EXTENSION_ROOT = path.resolve(HERE, "..");

// --- strict JSONL framing ---------------------------------------------------

/**
 * Split a buffer into complete JSONL records, LF only.
 *
 * pi frames with `\n` and its payload strings may contain other Unicode
 * separators (U+2028, U+2029) that are legal inside a JSON string. Splitting on
 * anything but `\n` - as Node's readline does - would tear a record in half. A
 * single trailing `\r` is stripped, matching pi's own reader.
 */
export function splitJsonlLines(buffer: string): { lines: string[]; rest: string } {
  const lines: string[] = [];
  let rest = buffer;
  while (true) {
    const index = rest.indexOf("\n");
    if (index === -1) break;
    const line = rest.slice(0, index);
    rest = rest.slice(index + 1);
    lines.push(line.endsWith("\r") ? line.slice(0, -1) : line);
  }
  return { lines, rest };
}

// --- stub model provider ----------------------------------------------------

/** One scripted model turn: either plain text or a single tool call. */
export interface StubTurn {
  text?: string;
  toolCall?: { name: string; arguments: Record<string, unknown> };
}

export interface StubProvider {
  readonly port: number;
  readonly baseUrl: string;
  /** Raw request bodies received, in order. */
  readonly requests: string[];
  close(): Promise<void>;
}

function sseChunk(delta: unknown, finishReason: string | null): string {
  return `data: ${JSON.stringify({
    id: "chatcmpl-stub",
    object: "chat.completion.chunk",
    created: 1,
    model: STUB_MODEL,
    choices: [{ index: 0, delta, finish_reason: finishReason }],
  })}\n\n`;
}

/**
 * An OpenAI-compatible endpoint that replays ``turns`` in order.
 *
 * The last turn repeats if the agent asks for more, so a script never runs dry
 * mid-run and a suite need only describe the turns it cares about.
 */
export async function startStubProvider(turns: StubTurn[]): Promise<StubProvider> {
  if (turns.length === 0) throw new Error("startStubProvider needs at least one turn");
  const requests: string[] = [];
  let served = 0;

  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      if (!req.url?.includes("chat/completions")) {
        res.writeHead(404, { "content-type": "application/json" }).end("{}");
        return;
      }
      requests.push(body);
      const turn = turns[Math.min(served, turns.length - 1)];
      served += 1;
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
      res.write(sseChunk({ role: "assistant", content: "" }, null));
      if (turn.toolCall) {
        res.write(
          sseChunk(
            {
              tool_calls: [
                {
                  index: 0,
                  id: `call_${served}`,
                  type: "function",
                  function: {
                    name: turn.toolCall.name,
                    arguments: JSON.stringify(turn.toolCall.arguments),
                  },
                },
              ],
            },
            null,
          ),
        );
        res.write(sseChunk({}, "tool_calls"));
      } else {
        res.write(sseChunk({ content: turn.text ?? "ok" }, null));
        res.write(sseChunk({}, "stop"));
      }
      res.write("data: [DONE]\n\n");
      res.end();
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") throw new Error("stub provider has no port");
  const port = address.port;

  return {
    port,
    baseUrl: `http://127.0.0.1:${port}/v1`,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}

// --- workspace --------------------------------------------------------------

export interface Workspace {
  /** Temp root holding everything pi reads and writes. */
  readonly root: string;
  /** Stand-in HOME, so `specflo extension install` targets it. */
  readonly home: string;
  /** pi's agent dir: `<home>/.pi/agent`. */
  readonly agentDir: string;
  /** The directory pi runs in. */
  readonly project: string;
  /** Where the extension was installed, if it was. */
  readonly extensionDir: string;
  cleanup(): void;
}

/**
 * A throwaway pi installation wired to ``provider``.
 *
 * With ``installExtension`` (the default) the real `specflo extension install`
 * runs against this workspace's HOME, so what pi discovers is what the shipped
 * installer produces - not a copy this harness arranged to its own liking.
 */
export function createWorkspace(
  provider: StubProvider,
  options: { installExtension?: boolean } = {},
): Workspace {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-pi-"));
  const home = path.join(root, "home");
  const agentDir = path.join(home, ".pi", "agent");
  const project = path.join(root, "project");
  fs.mkdirSync(agentDir, { recursive: true });
  fs.mkdirSync(project, { recursive: true });

  fs.writeFileSync(
    path.join(agentDir, "models.json"),
    JSON.stringify(
      {
        providers: {
          [STUB_PROVIDER]: {
            baseUrl: provider.baseUrl,
            api: "openai-completions",
            apiKey: "no-key",
            authHeader: false,
            models: [
              {
                id: STUB_MODEL,
                name: "specflo test stub",
                contextWindow: 100000,
                maxTokens: 4096,
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
              },
            ],
          },
        },
      },
      null,
      2,
    ),
  );
  // No `packages` entry: the extension must be found by pi's own discovery of
  // the extensions directory, which is the whole claim the installer rests on.
  fs.writeFileSync(path.join(agentDir, "settings.json"), JSON.stringify({ packages: [] }, null, 2));

  const extensionDir = path.join(agentDir, "extensions", "specflo");
  if (options.installExtension !== false) {
    execFileSync(SPECFLO_BIN, ["extension", "install"], {
      cwd: project,
      env: { ...process.env, HOME: home },
      encoding: "utf8",
    });
  }

  return {
    root,
    home,
    agentDir,
    project,
    extensionDir,
    cleanup: () => fs.rmSync(root, { recursive: true, force: true }),
  };
}

// --- pi session -------------------------------------------------------------

export type RpcEvent = Record<string, unknown> & { type: string };

export interface PiSession {
  /** Every parsed frame pi has emitted, in order. */
  readonly events: RpcEvent[];
  /** Anything pi wrote to stderr. */
  stderr(): string;
  /** Frames that failed to parse as JSON - always empty on a healthy run. */
  readonly unparsed: string[];
  send(command: Record<string, unknown>): void;
  waitFor(predicate: (event: RpcEvent) => boolean, timeoutMs?: number): Promise<RpcEvent>;
  /** Extension UI frames, optionally narrowed to one method. */
  uiRequests(method?: string): RpcEvent[];
  /** Terminate pi and resolve with its exit code. */
  close(): Promise<number | null>;
  /** Resolve when pi exits on its own. */
  exited(): Promise<number | null>;
}

/**
 * The argv the harness runs pi with.
 *
 * Exported so a test can assert what it never contains: `-p` / `--print`, the
 * one-shot mode this harness is defined against.
 */
export function piArgs(extra: string[] = []): string[] {
  return ["--mode", "rpc", "--provider", STUB_PROVIDER, "--model", STUB_MODEL, ...extra];
}

/** Spawn `pi --mode rpc` against ``workspace`` and stream its JSONL output. */
export function spawnPi(workspace: Workspace, extra: string[] = []): PiSession {
  const args = piArgs([
    "--session-dir",
    path.join(workspace.root, "sessions"),
    ...extra,
  ]);
  const child: ChildProcessWithoutNullStreams = spawn(PI_BIN, args, {
    cwd: workspace.project,
    env: {
      ...process.env,
      HOME: workspace.home,
      PI_CODING_AGENT_DIR: workspace.agentDir,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  const events: RpcEvent[] = [];
  const unparsed: string[] = [];
  let stderrText = "";
  let buffer = "";
  const watchers = new Set<() => void>();

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    const { lines, rest } = splitJsonlLines(buffer);
    buffer = rest;
    for (const line of lines) {
      if (line.trim() === "") continue;
      try {
        events.push(JSON.parse(line) as RpcEvent);
      } catch {
        unparsed.push(line);
      }
    }
    for (const notify of watchers) notify();
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => (stderrText += chunk));

  const exit = new Promise<number | null>((resolve) => {
    child.on("exit", (code) => {
      for (const notify of watchers) notify();
      resolve(code);
    });
  });

  return {
    events,
    unparsed,
    stderr: () => stderrText,
    send(command) {
      child.stdin.write(`${JSON.stringify(command)}\n`);
    },
    waitFor(predicate, timeoutMs = 30000) {
      return new Promise<RpcEvent>((resolve, reject) => {
        let settled = false;
        const check = () => {
          if (settled) return;
          const found = events.find(predicate);
          if (found) {
            settled = true;
            cleanup();
            resolve(found);
          }
        };
        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          cleanup();
          reject(
            new Error(
              `timed out after ${timeoutMs}ms; saw: ${events.map((e) => e.type).join(", ")}` +
                (stderrText ? `\nstderr: ${stderrText}` : ""),
            ),
          );
        }, timeoutMs);
        const cleanup = () => {
          clearTimeout(timer);
          watchers.delete(check);
        };
        watchers.add(check);
        check();
      });
    },
    uiRequests(method) {
      return events.filter(
        (event) =>
          event.type === "extension_ui_request" && (method === undefined || event.method === method),
      );
    },
    close() {
      child.kill("SIGTERM");
      return exit;
    },
    exited: () => exit,
  };
}
