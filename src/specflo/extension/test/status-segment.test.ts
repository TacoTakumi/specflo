/**
 * The specflo status segment computation (T-01, REQ-01 / REQ-02 / REQ-03).
 *
 * `computeSegment(cwd)` parses the specflo artifacts directly - config.yaml,
 * project.md frontmatter, plan.md task blocks - with exactly the rules of
 * `~/.claude/statusline.sh`'s specflo_seg, and returns the uncolored segment
 * text plus the style token the wiring should theme it with, or null when
 * there is nothing to show. The tests here pin the parse rules with a fixture
 * repo; the wiring that applies the result through ctx.ui.setStatus is tested
 * separately (status-wiring.test.ts, T-02/T-03).
 *
 * The fixture builder mirrors a real specflo repo: `.specflo/config.yaml`
 * naming projects_dir and active_project, a `project.md` with frontmatter, and
 * an optional `plan.md` whose `### T-NN` blocks carry the machine-managed
 * `- Progress:` / `- Status:` / `- Superseded by:` fields.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, it } from "node:test";

import { computeSegment } from "../src/index.ts";

interface FixtureOpts {
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
function createFixtureRepo(opts: FixtureOpts = {}): string {
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
const THREE_TASKS = [
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

const cleanups: string[] = [];
afterEach(() => {
  for (const root of cleanups.splice(0).reverse()) fs.rmSync(root, { recursive: true, force: true });
});

describe("computeSegment - repo discovery (REQ-01)", () => {
  it("resolves from the repo root and from a nested subdirectory to the same segment", () => {
    const root = createFixtureRepo();
    cleanups.push(root);
    const nested = path.join(root, "src", "deep", "dir");
    fs.mkdirSync(nested, { recursive: true });
    assert.deepEqual(computeSegment(nested), computeSegment(root));
    assert.deepEqual(computeSegment(root), { text: "demo:execute", style: "magenta" });
  });

  it("honours a custom projects_dir from config.yaml", () => {
    const root = createFixtureRepo({ projectsDir: "artifacts" });
    cleanups.push(root);
    assert.deepEqual(computeSegment(root), { text: "demo:execute", style: "magenta" });
  });
});

describe("computeSegment - nothing to show clears (REQ-02)", () => {
  it("returns null outside any specflo repo", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "specflo-status-empty-"));
    cleanups.push(root);
    assert.equal(computeSegment(root), null);
    assert.equal(computeSegment(path.join(root, "a", "b")), null);
  });

  it("returns null when config.yaml has no active_project", () => {
    const root = createFixtureRepo({ configExtra: [] });
    cleanups.push(root);
    fs.writeFileSync(path.join(root, ".specflo", "config.yaml"), "projects_dir: docs/projects\n");
    assert.equal(computeSegment(root), null);
  });

  it("returns null when project.md frontmatter is malformed", () => {
    const root = createFixtureRepo();
    cleanups.push(root);
    fs.writeFileSync(path.join(root, "docs", "projects", "demo", "project.md"), "# no frontmatter\n");
    assert.equal(computeSegment(root), null);
  });

  it("returns null when project.md is missing", () => {
    const root = createFixtureRepo();
    cleanups.push(root);
    fs.rmSync(path.join(root, "docs", "projects", "demo", "project.md"));
    assert.equal(computeSegment(root), null);
  });
});

describe("computeSegment - format table (REQ-03)", () => {
  const cases: Array<{ name: string; opts: FixtureOpts; expected: { text: string; style: string } | null }> = [
    {
      name: "brainstorm renders slug:phase",
      opts: { phase: "brainstorm" },
      expected: { text: "demo:brainstorm", style: "magenta" },
    },
    {
      name: "spec renders slug:phase",
      opts: { phase: "spec" },
      expected: { text: "demo:spec", style: "magenta" },
    },
    {
      name: "plan with an in-progress task shows its id and the tally",
      opts: { phase: "plan", plan: THREE_TASKS },
      expected: { text: "demo:plan T-02 1/3", style: "magenta" },
    },
    {
      name: "plan without an in-progress task shows the tally only",
      opts: { phase: "plan", plan: THREE_TASKS.replace("- Progress: in_progress", "- Progress: pending") },
      expected: { text: "demo:plan 1/3", style: "magenta" },
    },
    {
      name: "execute drops the phase label and shows the tally",
      opts: { phase: "execute", plan: THREE_TASKS },
      expected: { text: "demo T-02 1/3", style: "magenta" },
    },
    {
      name: "execute without an in-progress task shows the tally only",
      opts: { phase: "execute", plan: THREE_TASKS.replace("- Progress: in_progress", "- Progress: pending") },
      expected: { text: "demo 1/3", style: "magenta" },
    },
    {
      name: "complete renders 'slug done' dim",
      opts: { phase: "execute", status: "complete" },
      expected: { text: "demo done", style: "dim" },
    },
    {
      name: "shelved renders 'slug shelved' dim",
      opts: { phase: "execute", status: "shelved" },
      expected: { text: "demo shelved", style: "dim" },
    },
    {
      name: "a slug longer than 17 characters is truncated to 16 plus an ellipsis",
      opts: { slug: "dsv4-rs-rollback-rebase-20260804", phase: "execute" },
      expected: { text: "dsv4-rs-rollback\u2026:execute", style: "magenta" },
    },
    {
      name: "a superseded task is excluded from both counts (explicit field)",
      opts: {
        phase: "plan",
        plan: [
          "### T-01 - first",
          "- Progress: done",
          "- Status: active",
          "",
          "### T-02 - superseded",
          "- Progress: pending",
          "- Status: active",
          "- Superseded by: T-04",
          "",
          "### T-03 - third",
          "- Progress: in_progress",
          "- Status: active",
          "",
        ].join("\n"),
      },
      expected: { text: "demo:plan T-03 1/2", style: "magenta" },
    },
    {
      name: "the legacy 'Status: superseded by' phrase marks a task superseded",
      opts: {
        phase: "plan",
        plan: [
          "### T-01 - first",
          "- Progress: done",
          "- Status: active",
          "",
          "### T-02 - legacy superseded",
          "- Progress: in_progress",
          "- Status: superseded by T-05",
          "",
        ].join("\n"),
      },
      expected: { text: "demo:plan 1/1", style: "magenta" },
    },
    {
      name: "a plan whose tasks are all superseded shows no tally",
      opts: {
        phase: "plan",
        plan: [
          "### T-01 - superseded",
          "- Progress: done",
          "- Status: active",
          "- Superseded by: T-02",
          "",
        ].join("\n"),
      },
      expected: { text: "demo:plan", style: "magenta" },
    },
    {
      name: "a plan phase with no plan.md shows no tally",
      opts: { phase: "plan", plan: null },
      expected: { text: "demo:plan", style: "magenta" },
    },
  ];

  for (const c of cases) {
    it(c.name, () => {
      const root = createFixtureRepo(c.opts);
      cleanups.push(root);
      assert.deepEqual(computeSegment(root), c.expected);
    });
  }
});
