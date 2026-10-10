/**
 * specflo bench input log - appends every pi input event to a JSONL file.
 *
 * The model bench loads this file with `-e` on every pi run and names the log
 * in SPECFLO_INPUT_LOG. Each line is {ts, source, text}: `source` is pi's own
 * word for where the input came from (interactive for keys typed in the TUI,
 * rpc for a command on the RPC stream, extension for a message an extension
 * sent). The bench reads the log to tell an operator's typing in a run's pane
 * from the auto chain's own messages.
 *
 * pi runs an extension command (a line such as `/specflo-continue auto`)
 * before it emits the input event, so such a command is never logged. The
 * log never changes the input: the handler returns nothing, and a failed
 * write is dropped so it cannot disturb the run. With the variable unset the
 * extension does nothing.
 */

import { appendFileSync } from "node:fs";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** The variable the bench puts the log path in. */
export const INPUT_LOG_ENV = "SPECFLO_INPUT_LOG";

export default function (pi: ExtensionAPI, env: Record<string, string | undefined> = process.env) {
  const path = env[INPUT_LOG_ENV];
  if (!path) return;

  pi.on("input", (event) => {
    const line = { ts: new Date().toISOString(), source: event.source, text: event.text };
    try {
      appendFileSync(path, JSON.stringify(line) + "\n");
    } catch {
      // The log is evidence for the bench, never a reason to fail the run.
    }
    return undefined;
  });
}
