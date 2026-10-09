"""The `index.html` page of a graph output directory.

`write_index(out_dir)` reads every `<base>-pass-rate.png` and
`<base>-metrics.png` in the directory with the disclosure `.txt` beside it, and
writes one page: what the bench measures, how to read a graph, a section per
level with each comparison's arms as a table and its graphs, and what each
metric means. It reads the directory, not the records, so graphs written by
earlier `mb.py graph` runs into the same directory are on the page too.

The page is plain HTML with one inline stylesheet. Every colour is a token on
:root with the token names of the agent-wiki viewer (--bg, --fg, --accent and
so on), so a later design can replace the stylesheet alone.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from modelbench.arms import LEVELS

INDEX_NAME = "index.html"

GRAPH_KINDS = (
    ("pass-rate", "Pass rate"),
    ("metrics", "Behaviour metrics"),
)

LEVEL_TEXT = {
    "quick": "One small change: `tinytodo list` prints `No tasks.` when it has "
             "nothing to show, with a test. The seeded project is at specflo's "
             "quick level: one brief, one check.",
    "fast": "A small feature: optional due dates on tasks and a list of overdue "
            "tasks. The seeded project is at specflo's fast level: a short "
            "brainstorm, spec and plan of 3 to 7 tasks.",
    "full": "A set of features: tags, editing, search, stats and export. The "
            "seeded project is at specflo's full level: every phase, with an "
            "approval at each.",
}

# By the metric's first dotted part; a metric not named here shows with no text.
METRIC_TEXT = {
    "pass rate": "Held-out tests that pass over all held-out tests for the level, "
                 "from 0 to 1. The agent never sees these tests.",
    "turns": "Model requests that do the run's work (main agent and subagents). "
             "Harness overhead such as titles, summaries and compaction is not "
             "counted.",
    "output_tokens_per_turn": "Output tokens the model wrote per turn, as the "
                              "mean, the median and the largest turn of the run.",
    "tool_calls": "Calls of one tool, from every request. Tool names that differ "
                  "only in case (Claude Code's Bash, pi's bash) are one metric.",
    "tool_errors": "Tool calls whose result has the error flag.",
    "wall_time_s": "Seconds from the first event of the session to the last.",
}

_STYLE = """
:root {
  color-scheme: light dark;
  --bg:           light-dark(#ffffff, #17191c);
  --bg-muted:     light-dark(#f6f7f9, #1f2226);
  --fg:           light-dark(#1f2329, #dde0e5);
  --fg-muted:     light-dark(#5c6570, #9aa1ab);
  --border:       light-dark(#d5d9de, #353a41);
  --border-muted: light-dark(#e4e7eb, #2b2f35);
  --accent:       light-dark(#5b4fd6, #a99fff);
}
:root {
  --accent-soft: color-mix(in srgb, var(--accent) 12%, transparent);
  --radius: 6px;
  --gutter: 32px;
  --ui: system-ui, -apple-system, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
@media (max-width: 767px) { :root { --gutter: 16px; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.55 var(--ui); }
main { max-width: 1180px; margin: 0 auto; padding: 24px var(--gutter) 64px; }
h1, h2, h3 { line-height: 1.25; }
h2 { margin-top: 2.5em; padding-bottom: .3em; border-bottom: 1px solid var(--border); }
a { color: var(--accent); }
code { font-family: var(--mono); font-size: .9em; background: var(--bg-muted); padding: .1em .3em; border-radius: 4px; }
.muted { color: var(--fg-muted); }
nav ul { padding-left: 1.2em; }
dl.glossary dt { font-family: var(--mono); font-weight: 600; margin-top: .8em; }
dl.glossary dd { margin-left: 1.2em; }
.comparison { margin-top: 1.5em; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; font-size: 13px; margin: .5em 0 1em; }
th, td { border: 1px solid var(--border-muted); padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: var(--bg-muted); }
td { font-family: var(--mono); overflow-wrap: anywhere; min-width: 18ch; }
thead th { min-width: 18ch; }
tr.differs th, tr.differs td { background: var(--accent-soft); }
figure { margin: 1em 0 2em; }
figure img { max-width: 100%; height: auto; border: 1px solid var(--border); border-radius: var(--radius); }
figcaption { color: var(--fg-muted); margin-top: .4em; }
details { margin: .5em 0; }
pre { font-family: var(--mono); font-size: 12px; background: var(--bg-muted); padding: 8px; border-radius: var(--radius); overflow-x: auto; white-space: pre-wrap; }
"""


def _code(text: str) -> str:
    """Escape text and turn `backtick` spans into <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(text))


def parse_disclosure(text: str) -> tuple[dict[str, str], list[tuple[str, dict[str, str]]]] | None:
    """The head fields and each arm's fields of a disclosure, or None if it does not parse."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    head: dict[str, str] = {}
    for part in lines[0].split("; "):
        key, sep, value = part.partition(": ")
        if not sep:
            return None
        head[key] = value
    arms: list[tuple[str, dict[str, str]]] = []
    for line in lines[1:]:
        name, sep, rest = line.partition(": ")
        if not sep:
            return None
        fields: dict[str, str] = {}
        for part in rest.split("; "):
            key, sep, value = part.partition("=")
            if not sep:
                return None
            fields[key] = value
        arms.append((name, fields))
    return head, arms


def _arms_table(arms: list[tuple[str, dict[str, str]]]) -> str:
    keys = list(dict.fromkeys(k for _, fields in arms for k in fields))
    head = "".join(f"<th>{escape(name)}</th>" for name, _ in arms)
    rows = []
    for key in keys:
        values = [fields.get(key, "") for _, fields in arms]
        cls = ' class="differs"' if len(set(values)) > 1 else ""
        cells = "".join(f"<td>{escape(v)}</td>" for v in values)
        rows.append(f"<tr{cls}><th>{escape(key)}</th>{cells}</tr>")
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr><th>field</th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _comparison(out: Path, base: str, kinds: list[str]) -> str:
    txt = out / f"{base}-{kinds[0]}.txt"
    text = txt.read_text() if txt.exists() else ""
    parsed = parse_disclosure(text)
    parts = [f'<section class="comparison" id="{escape(base)}">', f"<h3>{escape(base)}</h3>"]
    if parsed:
        head, arms = parsed
        summary = "; ".join(f"{k}: {v}" for k, v in head.items() if k != "level")
        parts.append(f'<p class="muted">{escape(summary)}</p>')
        if arms:
            parts.append(
                "<p>The arms of this comparison. A highlighted row is a field "
                "that differs between the arms: the compared variable.</p>"
            )
            parts.append(_arms_table(arms))
    elif not text:
        parts.append('<p class="muted">No disclosure file beside these graphs.</p>')
    for kind, label in GRAPH_KINDS:
        if kind not in kinds:
            continue
        name = f"{base}-{kind}.png"
        parts.append(
            f'<figure><a href="{escape(name)}"><img src="{escape(name)}" '
            f'alt="{escape(label)}: {escape(base)}" loading="lazy"></a>'
            f"<figcaption>{escape(label)}. Open the image for full size.</figcaption></figure>"
        )
    if text:
        parts.append(
            "<details><summary>Disclosure text</summary>"
            f"<pre>{escape(text)}</pre></details>"
        )
    parts.append("</section>")
    return "\n".join(parts)


def _graph_bases(out: Path) -> dict[str, dict[str, list[str]]]:
    """{level: {base name: [graph kinds]}} for the PNGs in the directory."""
    found: dict[str, dict[str, list[str]]] = {}
    for png in sorted(out.glob("*.png")):
        for kind, _ in GRAPH_KINDS:
            suffix = f"-{kind}.png"
            if png.name.endswith(suffix):
                base = png.name[: -len(suffix)]
                level = base.split("-", 1)[0]
                found.setdefault(level, {}).setdefault(base, []).append(kind)
                break
    return found


def render_index(out_dir: Path | str) -> str:
    out = Path(out_dir)
    bases = _graph_bases(out)
    levels = [lv for lv in LEVELS if lv in bases] + sorted(lv for lv in bases if lv not in LEVELS)
    toc = "".join(f'<li><a href="#level-{escape(lv)}">{escape(lv)}</a></li>' for lv in levels)
    glossary = "".join(
        f"<dt>{escape(name)}</dt><dd>{_code(text)}</dd>" for name, text in METRIC_TEXT.items()
    )
    sections = []
    for level in levels:
        sections.append(f'<h2 id="level-{escape(level)}">Level: {escape(level)}</h2>')
        if level in LEVEL_TEXT:
            sections.append(f"<p>{_code(LEVEL_TEXT[level])}</p>")
        for base, kinds in sorted(bases[level].items()):
            sections.append(_comparison(out, base, kinds))
    body = "\n".join(sections) or '<p class="muted">No graphs in this directory.</p>'
    title = f"Model bench: {out.name}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
<p>The specflo model bench measures how well a local model drives specflo. Each
run seeds a specflo project in a small Python repo (tinytodo, a to-do CLI), runs
it unattended under <code>specflo auto</code>, and grades the final tree with a
held-out test suite the agent never sees. See <code>BENCH.md</code> in the
specflo repo for the full method.</p>
<ul>
<li>An <strong>arm</strong> is one llama-swap entry (a model, its engine and
GGUF) and one harness (pi or Claude Code).</li>
<li>A <strong>level</strong> is the size of the seeded work: quick, fast or
full.</li>
<li>A <strong>comparison</strong> puts arms side by side that differ in one
variable only: the entry or the harness. Every other setting is the same.</li>
<li>Invalid runs (wrong model id, or a tree that could not be graded) and
contaminated runs (a tool call named the bench or the held-out tests) are left
out. The disclosure counts them.</li>
</ul>

<h2 id="reading">How to read a graph</h2>
<ul>
<li>Each panel has one bar per arm. The bar is the mean over the arm's included
runs.</li>
<li>Each dot is one run. The spread of the dots is the run-to-run spread.</li>
<li>The pass-rate graph has one panel: the held-out pass rate. The metrics graph
has one panel per behaviour metric.</li>
<li>The text under each plot is the disclosure: the level, the compared
variable, the excluded runs and every recorded field of each arm with its run
count. The table above the graphs shows the same fields.</li>
<li>Few runs per arm give a weak mean. Read the run count before the bar.</li>
</ul>

<nav><strong>Levels</strong><ul>{toc}<li><a href="#metrics">What the metrics mean</a></li></ul></nav>

{body}

<h2 id="metrics">What the metrics mean</h2>
<p>A panel name with a dot is one part of a metric: <code>tool_calls.bash</code>
is the bash calls, <code>output_tokens_per_turn.mean</code> is the mean.</p>
<dl class="glossary">{glossary}</dl>
</main>
</body>
</html>
"""


def write_index(out_dir: Path | str) -> Path:
    """Write `index.html` for the graphs in the directory; return its path."""
    path = Path(out_dir) / INDEX_NAME
    path.write_text(render_index(out_dir))
    return path
