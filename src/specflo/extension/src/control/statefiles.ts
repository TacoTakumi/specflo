/**
 * Disk writers for the per-agent state layout, matching the v1 Python
 * subsystem's statefiles module byte-shape for byte-shape (REQ-07):
 *
 *   - events.jsonl: append-only, one JSON object per line, a ``ts`` field
 *     (write time, UTC, millisecond ISO 8601) placed first, the event's own
 *     fields following unchanged.
 *   - status.json: atomic replace - write a sibling tmp file, then rename -
 *     so a reader never sees a torn snapshot.
 *
 * Shared transport utilities in REQ-14's sense: Node built-ins only, no
 * continuation-loop imports.
 */

import * as fs from "node:fs";

/** UTC now in v1's format: ISO 8601, millisecond precision, +00:00 suffix. */
export function nowIso(): string {
  return new Date().toISOString().replace("Z", "+00:00");
}

/** Append one timestamped JSON line, v1 EventLog-shaped. */
export function appendEvent(path: string, event: Record<string, unknown>): void {
  fs.appendFileSync(path, `${JSON.stringify({ ts: nowIso(), ...event })}\n`);
}

/** Atomically replace ``path`` with ``snapshot``, the v1 tmp-then-rename dance. */
export function writeStatusFile(path: string, snapshot: Record<string, unknown>): void {
  const tmp = `${path}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, `${JSON.stringify(snapshot, null, 2)}\n`);
  fs.renameSync(tmp, path);
}
