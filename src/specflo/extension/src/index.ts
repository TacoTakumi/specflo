/**
 * specflo - a thin pi extension that drives specflo's continuation loop.
 *
 * Everything this extension knows it learns by running the `specflo` binary and
 * relaying its stdout byte for byte. It opens no project artifact, keeps no
 * durable state, registers no model-callable tool, and blocks no tool call.
 *
 * The behaviour lands task by task:
 *   - cold start: fetch `specflo hook reseed` and inject it on the first turn
 *   - arming:     watch context usage against the threshold `status --json` reports
 *   - seam:       poll `status --json` while armed for a phase or task change
 *   - attended:   one passive notice per seam, via ctx.ui.notify only
 *   - unattended: clear and reseed at a seam while an auto run is under way
 *
 * This file is the single extension entry point named by package.json's
 * `pi.extensions`.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function specflo(pi: ExtensionAPI): void {
  // Handlers are registered by the tasks listed above. Loading this extension
  // must stay side-effect free: pi discovers and loads it in every session, so
  // an empty registration is the correct no-op until a handler lands.
  void pi;
}
