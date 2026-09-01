/**
 * herdr state reporting for a managed pane (REQ-08).
 *
 * When the managed-start handshake carries a pane id, the control surface
 * pushes real lifecycle state into herdr - `pane report-agent` with a
 * monotonic seq, working during a run, idle at settle, blocked while a UI
 * prompt is open - and releases the registration at shutdown. The argv
 * mirrors the v1 host's herdr adapter exactly (pane id first, --source,
 * --agent, --state, --seq).
 *
 * Every push is fire-and-forget and best-effort: a missing or failing herdr
 * is reported through ``onError`` (the caller logs it as an event line, as
 * the v1 host does) and never disturbs the session.
 */

import { execFile } from "node:child_process";

/** The handshake key carrying the pane id (set by managed starts alone). */
export const ENV_AGENT_PANE = "SPECFLO_AGENT_PANE";

/** Test/deploy override for the herdr binary. */
export const ENV_HERDR_BIN = "SPECFLO_HERDR_BIN";

/** The lifecycle-authority source this reporter registers under. */
export const HERDR_SOURCE = "specflo-pi-extension";

export type HerdrState = "working" | "idle" | "blocked";

export interface HerdrReporter {
  report(state: HerdrState): void;
  release(): void;
}

/**
 * A reporter for ``paneId``, or null when the handshake carries no pane -
 * the no-pane, no-push, no-errors case.
 */
export function createReporter(options: {
  paneId: string | undefined;
  agent: string;
  env?: Record<string, string | undefined>;
  onError: (message: string) => void;
}): HerdrReporter | null {
  const { paneId, agent, onError } = options;
  if (!paneId) return null;
  const bin = (options.env ?? process.env)[ENV_HERDR_BIN] ?? "herdr";
  let seq = 0;

  const run = (args: string[], what: string): void => {
    execFile(bin, args, { encoding: "utf8" }, (error) => {
      if (error !== null) onError(`herdr ${what}: ${error.message}`);
    });
  };

  return {
    report(state: HerdrState) {
      seq += 1;
      run(
        [
          "pane",
          "report-agent",
          paneId,
          "--source",
          HERDR_SOURCE,
          "--agent",
          agent,
          "--state",
          state,
          "--seq",
          String(seq),
        ],
        "report",
      );
    },
    release() {
      run(
        ["pane", "release-agent", paneId, "--source", HERDR_SOURCE, "--agent", agent],
        "release",
      );
    },
  };
}
