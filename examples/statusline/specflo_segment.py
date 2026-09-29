#!/usr/bin/env python3
"""Print the specflo segment for a statusline.

    specflo_segment.py [DIR]

Shows the active specflo project of the repo that holds DIR (default: the
current directory), for example:

    my-project:brainstorm      a project in brainstorm or spec
    my-project:plan 0/5        in plan, with the task count
    my-project T-03 2/5        in execute: the task in progress, done/total
    my-project done            a complete project (dim)
    my-project shelved         a shelved project (dim)

It prints nothing when there is no specflo repo or no active project, and it
always exits 0, so a statusline never breaks on it. SPECFLO_DIRECTORY, when
set, is where it looks instead of DIR, as specflo itself does. NO_COLOR turns
the colour off.

The segment is read straight from specflo's files (.specflo/config.yaml, the
project's project.md and plan.md), with the same rules as the status segment
of specflo's pi extension. It never runs the specflo command, which would cost
about 0.15 s per redraw. A project hosted on a daemon has no local files, so it
shows nothing.

Import it to use it from your own Python statusline:

    import specflo_segment
    text = specflo_segment.segment("/path/in/repo")   # None when nothing to show
"""

import os
import re
import sys

MAGENTA = "35"
DIM = "2"
RESET = "\033[0m"

# A slug longer than this is cut to one character less plus an ellipsis.
MAX_LABEL = 17


def _color(text, code, color):
    return "\033[{}m{}{}".format(code, text, RESET) if color else text


def _open(path):
    # UTF-8 with replacement, as the pi extension reads: a stray byte in a
    # file must not hide the segment.
    return open(path, encoding="utf-8", errors="replace")


def _find_root(start):
    root = os.path.realpath(start)
    if not os.path.isdir(root):
        return None
    while not os.path.isfile(os.path.join(root, ".specflo", "config.yaml")):
        parent = os.path.dirname(root)
        if parent == root:
            return None
        root = parent
    return root


def _read_config(root):
    cfg = {}
    with _open(os.path.join(root, ".specflo", "config.yaml")) as f:
        for line in f:
            m = re.match(r"^(projects_dir|active_project):\s*(.+?)\s*$", line)
            if m:
                cfg[m.group(1)] = m.group(2).strip("'\"")
    return cfg


def _task_tally(plan_path):
    """(task in progress or None, done, total) over the tasks not superseded."""
    tasks, cur = [], None
    try:
        with _open(plan_path) as f:
            for line in f:
                m = re.match(r"^### (T-\d+)", line)
                if m:
                    cur = {"id": m.group(1), "progress": "pending", "sup": False}
                    tasks.append(cur)
                elif line.startswith("### "):
                    cur = None
                elif cur is not None:
                    m = re.match(r"^- (Progress|Status|Superseded by):\s*(.+?)\s*$", line)
                    if m:
                        if m.group(1) == "Progress":
                            cur["progress"] = m.group(2)
                        elif m.group(1) == "Superseded by" or "superseded by" in m.group(2):
                            cur["sup"] = True
    except OSError:
        return None
    active = [t for t in tasks if not t["sup"]]
    if not active:
        return None
    wip = next((t["id"] for t in active if t["progress"] == "in_progress"), None)
    done = sum(1 for t in active if t["progress"] == "done")
    return wip, done, len(active)


def segment(start, color=None):
    """The segment for the repo that holds `start`, or None.

    `color` defaults to on unless NO_COLOR is set. Never raises.
    """
    if color is None:
        color = not os.environ.get("NO_COLOR")
    try:
        root = _find_root(os.environ.get("SPECFLO_DIRECTORY") or start)
        if root is None:
            return None
        cfg = _read_config(root)
        slug = cfg.get("active_project")
        if not slug:
            return None
        label = slug if len(slug) <= MAX_LABEL else slug[:MAX_LABEL - 1] + "…"
        pdir = os.path.join(root, cfg.get("projects_dir", "docs/projects"), slug)
        try:
            with _open(os.path.join(pdir, "project.md")) as f:
                head = f.read(2048)
        except OSError:
            return None
        fm = re.match(r"\A---\r?\n(.*?)\r?\n---", head, re.S)
        meta = dict(re.findall(r"^(phase|status):\s*['\"]?([\w-]+)", fm.group(1), re.M)) if fm else {}
        phase = meta.get("phase")
        if not phase:
            return None
        if meta.get("status") == "complete":
            return _color("{} done".format(label), DIM, color)
        if meta.get("status") == "shelved":
            return _color("{} shelved".format(label), DIM, color)
        seg = "{}:{}".format(label, phase)
        if phase in ("plan", "execute"):
            tally = _task_tally(os.path.join(pdir, "plan.md"))
            if tally:
                wip, done, total = tally
                tail = (" " + wip if wip else "") + " {}/{}".format(done, total)
                # In execute the tally implies the phase, so drop it for width.
                seg = label + tail if phase == "execute" else seg + tail
        return _color(seg, MAGENTA, color)
    except Exception:
        return None


def main(argv):
    start = argv[1] if len(argv) > 1 else os.getcwd()
    text = segment(start)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
