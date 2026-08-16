/**
 * Fixture builder for the status segment suites: a real-shaped specflo repo
 * in a fresh temp dir - `.specflo/config.yaml` naming projects_dir and
 * active_project, a `project.md` with frontmatter, and an optional `plan.md`
 * whose `### T-NN` blocks carry the machine-managed `- Progress:` /
 * `- Status:` / `- Superseded by:` fields.
 *
 * Shared by status-segment.test.ts (the pure computation) and
 * status-wiring.test.ts (the ctx.ui.setStatus application). Not a test suite
 * itself, so the node --test barrel in index.js never loads it.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface FixtureRepoOpts {
  slug?: string;
  projectsDir?: string;
  phase?: string;
  status?: string;
  /** plan.md body; null (the default) writes no plan.md at all. */
  plan?: string | null;
  /** Extra config.yaml lines, e.g. to drop active_project for a REQ-02 case. */
  configExtra?: string[];
}

/** Builds a fixture specflo repo in a fresh temp dir and returns its root. */
export function createFixtureRepo(opts: FixtureRepoOpts = {}): string {
  const slug = opts.slug ?? "demo";
  const projectsDir = opts.projectsDir ?? "docs/projects";
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-status-"));
  fs.mkdirSync(path.join(root, ".specflo"), { recursive: true });
  const cfg = [
    "projects_dir: " + projectsDir,
    "active_project: " + slug,
    ...(opts.configExtra ?? []),
  ].join("\n");
  fs.writeFileSync(path.join(root, ".specflo", "config.yaml"), cfg + "\n");
  const pdir = path.join(root, projectsDir, slug);
  fs.mkdirSync(pdir, { recursive: true });
  fs.writeFileSync(
    path.join(pdir, "project.md"),
    `---\nname: ${slug}\nslug: ${slug}\nphase: ${opts.phase ?? "execute"}\nstatus: ${opts.status ?? "active"}\n---\n\n# ${slug}\n`,
  );
  if (opts.plan !== undefined && opts.plan !== null) {
    fs.writeFileSync(path.join(pdir, "plan.md"), opts.plan);
  }
  return root;
}

/** A plan.md body: three active tasks, one done and one in_progress. */
export const THREE_TASKS = [
  "### T-01 - first",
  "- Progress: done",
  "- Status: active",
  "",
  "### T-02 - second",
  "- Progress: in_progress",
  "- Status: active",
  "",
  "### T-03 - third",
  "- Progress: pending",
  "- Status: active",
  "",
].join("\n");
