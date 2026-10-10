/**
 * specflo bench input log - appends every pi input event, and the operator
 * actions pi reports as events, to a JSONL file.
 *
 * The model bench loads this file with `-e` on every pi run and names the log
 * in SPECFLO_INPUT_LOG. Each line is {ts, kind, source, text}. For kind
 * `input`, `source` is pi's own word for where the input came from
 * (interactive for keys typed in the TUI, rpc for a command on the RPC stream,
 * extension for a message an extension sent). The other kinds are actions
 * only an operator takes, logged with source interactive: `bash` (a `!` or
 * `!!` command), `model` (a model set or cycled; pi's own restore at a session
 * start is left out) and `thinking` (a new thinking level). The bench reads
 * the log to tell an operator's hand in a run's pane from the auto chain.
 *
 * pi handles slash commands (its own, such as /new or /compact, and extension
 * commands such as `/specflo-continue auto`) with no event an extension can
 * tell from the auto chain's, so they are never logged. The log never changes
 * what it sees: every handler returns nothing, and a failed write is dropped
 * so it cannot disturb the run. With the variable unset the extension does
 * nothing.
 */

import { appendFileSync } from "node:fs";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** The variable the bench puts the log path in. */
export const INPUT_LOG_ENV = "SPECFLO_INPUT_LOG";

export default function (pi: ExtensionAPI, env: Record<string, string | undefined> = process.env) {
  const path = env[INPUT_LOG_ENV];
  if (!path) return;

  const log = (kind: string, source: string, text: string): undefined => {
    const line = { ts: new Date().toISOString(), kind, source, text };
    try {
      appendFileSync(path, JSON.stringify(line) + "\n");
    } catch {
      // The log is evidence for the bench, never a reason to fail the run.
    }
    return undefined;
  };

  pi.on("input", (event) => log("input", event.source, event.text));
  pi.on("user_bash", (event) => log("bash", "interactive", event.command));
  pi.on("model_select", (event) => {
    if (event.source !== "restore") log("model", "interactive", `${event.model.provider}/${event.model.id}`);
  });
  pi.on("thinking_level_select", (event) => log("thinking", "interactive", event.level));
}
