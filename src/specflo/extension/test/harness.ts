/**
 * A fake pi runtime for the control-surface module: enough `ExtensionAPI` and
 * `ExtensionContext` to drive the control module's handlers directly, with no
 * pi process and no model.
 *
 * Separate from fake-ctx.ts on purpose: the control surface is content-agnostic
 * (REQ-14) - it never shells out to the specflo binary and never touches the
 * continuation loop - so its harness carries none of that machinery. What it
 * does carry:
 *
 *   - events:    `emit` runs every handler registered for an event type and
 *                logs each handler invocation, so a test can assert a module's
 *                hooks actually ran, not merely registered
 *   - capture:   `api.sendUserMessage` calls are recorded for later assertion
 *   - lifecycle: `startSession` / `shutdownSession` emit correctly-shaped
 *                session_start / session_shutdown events against the harness ctx
 */

export type Handler = (event: any, ctx: any) => unknown;

export interface HarnessOptions {
  /** The session working directory the fake ctx reports. */
  cwd?: string;
  /** The session display name the fake session manager reports. */
  sessionName?: string;
}

export interface ControlHarness {
  /** The object handed to the control module's register function. */
  api: any;
  /** Handlers registered per event name, in registration order. */
  handlers: Map<string, Handler[]>;
  /** Commands registered by name. */
  commands: Map<string, any>;
  /** User messages queued via api.sendUserMessage, in order. */
  sent: Array<{ content: unknown; options: unknown }>;
  /** One entry per handler run by `emit`, in execution order. */
  invoked: Array<{ event: string }>;
  /** The fake ExtensionContext every emitted event is handled against. */
  ctx: any;
  /** Every ctx.ui call, in order, as `{ method, args }`. */
  uiCalls: Array<{ method: string; args: unknown[] }>;
  /** What ctx.isIdle() reports; flip to false to fake a streaming agent. */
  idle: boolean;
  /** ctx.abort() invocations, in order. */
  abortCalls: unknown[];
  /** Invoke every handler for ``event.type`` and return the last result. */
  emit(event: { type: string } & Record<string, unknown>): Promise<unknown>;
  /** Emit a session_start with the given reason (default "startup"). */
  startSession(reason?: string): Promise<void>;
  /** Emit a session_shutdown with the given reason (default "quit"). */
  shutdownSession(reason?: string): Promise<void>;
}

export function createControlHarness(options: HarnessOptions = {}): ControlHarness {
  const handlers = new Map<string, Handler[]>();
  const commands = new Map<string, any>();
  const sent: Array<{ content: unknown; options: unknown }> = [];
  const invoked: Array<{ event: string }> = [];
  const uiCalls: Array<{ method: string; args: unknown[] }> = [];

  const api = {
    on(event: string, handler: Handler) {
      const existing = handlers.get(event) ?? [];
      existing.push(handler);
      handlers.set(event, existing);
    },
    registerCommand(name: string, cmdOptions: unknown) {
      commands.set(name, cmdOptions);
    },
    sendUserMessage(content: unknown, sendOptions: unknown) {
      sent.push({ content, options: sendOptions });
    },
  };

  const abortCalls: unknown[] = [];

  const ctx = {
    cwd: options.cwd ?? process.cwd(),
    ui: {
      notify(message: string, type?: string) {
        uiCalls.push({ method: "notify", args: [message, type] });
      },
    },
    isIdle: () => harness.idle,
    abort() {
      abortCalls.push(true);
    },
    sessionManager: {
      getSessionName: () => options.sessionName,
      getSessionId: () => "fake-session",
      getSessionFile: () => undefined,
    },
    model: undefined,
  };

  async function emit(event: { type: string } & Record<string, unknown>): Promise<unknown> {
    let last: unknown;
    for (const handler of handlers.get(event.type) ?? []) {
      invoked.push({ event: event.type });
      last = await handler(event, ctx);
    }
    return last;
  }

  const harness: ControlHarness = {
    api,
    handlers,
    commands,
    sent,
    invoked,
    ctx,
    uiCalls,
    idle: true,
    abortCalls,
    emit,
    async startSession(reason = "startup") {
      await emit({ type: "session_start", reason });
    },
    async shutdownSession(reason = "quit") {
      await emit({ type: "session_shutdown", reason });
    },
  };
  return harness;
}
