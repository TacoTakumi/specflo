/**
 * A fake pi runtime: enough `ExtensionAPI` and `ExtensionContext` to drive the
 * extension's handlers directly, with no pi process and no model.
 *
 * The unit layer exists so every requirement keeps an assertion that needs
 * neither. The end-to-end suites prove the extension behaves inside a real pi;
 * these prove exactly what each handler returns, for inputs a live run cannot
 * be steered into on demand.
 *
 * The `specflo` binary is faked too, and deliberately as a real executable
 * rather than a stubbed function: the extension shells out for every piece of
 * state, so the argv it passes and the bytes it reads back are the behaviour
 * under test. The fake records each invocation's arguments and replays a
 * canned stdout byte for byte.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

// --- fake extension API -----------------------------------------------------

export type Handler = (event: any, ctx: any) => unknown;

export interface FakePi {
  /** The object handed to the extension factory. */
  api: any;
  /** Handlers registered per event name, in registration order. */
  handlers: Map<string, Handler[]>;
  /** Commands registered by name. */
  commands: Map<string, any>;
  /** Tools registered by name - must stay empty (REQ-15). */
  tools: string[];
  /** User messages queued via api.sendUserMessage, for followUp assertions. */
  sent: Array<{ content: unknown; options: unknown }>;
  /** Invoke every handler for ``event.type`` and return the last result. */
  emit(event: { type: string } & Record<string, unknown>, ctx?: any): Promise<unknown>;
}

export function createFakePi(): FakePi {
  const handlers = new Map<string, Handler[]>();
  const commands = new Map<string, any>();
  const tools: string[] = [];
  const sent: Array<{ content: unknown; options: unknown }> = [];

  const api = {
    on(event: string, handler: Handler) {
      const existing = handlers.get(event) ?? [];
      existing.push(handler);
      handlers.set(event, existing);
    },
    registerCommand(name: string, options: unknown) {
      commands.set(name, options);
    },
    registerTool(tool: { name: string }) {
      tools.push(tool.name);
    },
    registerShortcut() {},
    sendUserMessage(content: unknown, options: unknown) {
      sent.push({ content, options });
    },
  };

  return {
    api,
    handlers,
    commands,
    tools,
    sent,
    async emit(event, ctx = createFakeCtx()) {
      let last: unknown;
      for (const handler of handlers.get(event.type) ?? []) {
        last = await handler(event, ctx);
      }
      return last;
    },
  };
}

// --- fake extension context -------------------------------------------------

/**
 * What a replacement factory returns: at minimum the context pi hands to the
 * ``withSession`` callback. Typed loosely on purpose - the rich factory
 * (`createFakeReplacement`) lives in auto-helpers.ts, which imports this
 * module, so naming its type here would close a cycle.
 */
export interface FakeReplacementLike {
  ctx: any;
}

/** Builds the replacement session ``newSession`` hands to ``withSession``. */
export type ReplacementFactory = (cwd: string) => FakeReplacementLike;

export interface FakeCtxOptions {
  cwd?: string;
  contextUsage?: { tokens: number | null; contextWindow: number; percent: number | null } | undefined;
  /**
   * Makes ``newSession`` behave the way pi does: build a replacement session
   * and run the caller's ``withSession`` against it before resolving. Left
   * unset, ``newSession`` only records - which is all a context that is never
   * expected to clear needs.
   */
  replacement?: ReplacementFactory;
}

export interface FakeCtx {
  cwd: string;
  mode: string;
  hasUI: boolean;
  ui: {
    notify(message: string, type?: string): void;
    confirm(...args: unknown[]): Promise<boolean>;
    select(...args: unknown[]): Promise<string | undefined>;
    input(...args: unknown[]): Promise<string | undefined>;
    /** Every ui call, in order, as `{ method, args }`. */
    calls: Array<{ method: string; args: unknown[] }>;
  };
  getContextUsage(): FakeCtxOptions["contextUsage"];
  isIdle(): boolean;
  /** Sessions started via the command-context action, for REQ-05/REQ-13 counting. */
  newSessionCalls: unknown[];
  /** The replacement each newSession built, in order - empty without a factory. */
  replacements: any[];
  newSession(options?: any): Promise<void>;
  /** ctx.abort() invocations, for REQ-01/REQ-06 counting. */
  abortCalls: unknown[];
  abort(): void;
}

export function createFakeCtx(options: FakeCtxOptions = {}): FakeCtx {
  const calls: Array<{ method: string; args: unknown[] }> = [];
  const newSessionCalls: unknown[] = [];
  const replacements: any[] = [];
  const abortCalls: unknown[] = [];
  const cwd = options.cwd ?? process.cwd();
  return {
    cwd,
    mode: "rpc",
    hasUI: true,
    ui: {
      calls,
      notify(message: string, type?: string) {
        calls.push({ method: "notify", args: [message, type] });
      },
      async confirm(...args: unknown[]) {
        calls.push({ method: "confirm", args });
        return false;
      },
      async select(...args: unknown[]) {
        calls.push({ method: "select", args });
        return undefined;
      },
      async input(...args: unknown[]) {
        calls.push({ method: "input", args });
        return undefined;
      },
    },
    getContextUsage: () => options.contextUsage,
    isIdle: () => true,
    newSessionCalls,
    replacements,
    async newSession(sessionOptions?: any) {
      newSessionCalls.push(sessionOptions);
      // pi's own ordering: the replacement session exists and the caller's
      // withSession callback has run against it before newSession resolves.
      // Whatever that callback leaves pending - a dispatched, un-awaited send -
      // is still pending when the caller continues, exactly as on real pi.
      if (options.replacement === undefined) return;
      const replacement = options.replacement(cwd);
      replacements.push(replacement);
      if (sessionOptions?.withSession) await sessionOptions.withSession(replacement.ctx);
    },
    abortCalls,
    abort() {
      abortCalls.push(true);
    },
  };
}

// --- fake specflo binary ----------------------------------------------------

export interface FakeSpecflo {
  /** Directory to remove when the test is done. */
  readonly root: string;
  /** Absolute path to point `SPECFLO_BIN` at. */
  readonly bin: string;
  /** One entry per invocation: the argv the extension passed, joined by spaces. */
  invocations(): string[];
  /** Replace the stdout replayed by the next invocations. */
  setStdout(text: string): void;
  cleanup(): void;
}

/**
 * A stand-in `specflo` executable that logs its argv and replays ``stdout``.
 *
 * `printf %s` rather than `echo`, so the replayed bytes are exactly the ones
 * given - no appended newline of the fake's own to muddy a byte comparison.
 */
export function createFakeSpecflo(stdout = ""): FakeSpecflo {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-fake-"));
  const bin = path.join(root, "specflo");
  const log = path.join(root, "argv.log");
  const out = path.join(root, "stdout.txt");
  fs.writeFileSync(out, stdout);
  fs.writeFileSync(
    bin,
    `#!/bin/sh\nprintf '%s\\n' "$*" >> ${JSON.stringify(log)}\ncat ${JSON.stringify(out)}\n`,
  );
  fs.chmodSync(bin, 0o755);

  return {
    root,
    bin,
    invocations() {
      if (!fs.existsSync(log)) return [];
      return fs.readFileSync(log, "utf8").split("\n").filter((line) => line !== "");
    },
    setStdout(text: string) {
      fs.writeFileSync(out, text);
    },
    cleanup() {
      fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

/**
 * Load the extension factory with ``SPECFLO_BIN`` pointed at ``bin``.
 *
 * The variable stays set until :func:`clearSpecfloBin`, because the extension
 * resolves the binary per call and the handlers run well after this returns.
 * Each factory call gets its own closure state, so one module instance is
 * enough. The end-to-end harness strips this variable from every child
 * environment, so a value left set here can never reach a real pi or a real
 * `specflo extension install`.
 */
export async function loadExtension(bin: string): Promise<(pi: any) => void> {
  process.env.SPECFLO_BIN = bin;
  const module = await import("../src/index.ts");
  return module.default;
}

/** Undo :func:`loadExtension`'s environment override. */
export function clearSpecfloBin(): void {
  delete process.env.SPECFLO_BIN;
}
