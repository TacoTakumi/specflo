"""Comparison check and graphs with disclosures, from run records.

An arm is a (llama-swap entry, harness) pair. The arms of one comparison may
differ in exactly one variable: the entry (with its engine, engine build and
GGUF) or the harness (with its version). Any other differing field - a
setting, the specflo version, or a field of the variable not compared - is
refused, as is a field that differs between runs of the same arm.

Invalid runs (`valid` false) and contaminated runs (`diagnostics.contaminated`
truthy) are left out of every pass rate and graph; the disclosure counts them.
Each graph is a PNG with its disclosure drawn under the plot and also written
beside it as a .txt file. matplotlib (the `bench` dependency group) is
imported only when drawing.
"""

from __future__ import annotations

import json
import math
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modelbench.arms import LEVELS
from modelbench.record import validate_record

# The fields that move with each comparable variable.
VARIABLES: dict[str, tuple[str, ...]] = {
    "entry": (
        "arm.entry",
        "engine",
        "versions.entry",
        "versions.engine_build",
        "versions.gguf_path",
    ),
    "harness": ("arm.harness", "versions.harness"),
}
_VARIABLE_OF = {f: var for var, fields in VARIABLES.items() for f in fields}


class GraphError(ValueError):
    """Graphs cannot be made from these records or this arm selection."""


class ComparisonError(GraphError):
    """Arms that differ in more than the compared variable. `fields` names them."""

    def __init__(self, fields: list[str], message: str) -> None:
        self.fields = fields
        super().__init__(message)


@dataclass
class ArmSummary:
    entry: str
    harness: str
    fields: dict[str, Any]
    scores: list[float] = field(default_factory=list)
    metrics: list[dict[str, float]] = field(default_factory=list)

    @property
    def run_count(self) -> int:
        return len(self.scores)

    @property
    def key(self) -> tuple[str, str]:
        return (self.entry, self.harness)


def is_excluded(rec: dict) -> bool:
    """True for an invalid or contaminated run."""
    return not rec.get("valid") or bool((rec.get("diagnostics") or {}).get("contaminated"))


def split_runs(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split records into (included, excluded)."""
    included = [r for r in records if not is_excluded(r)]
    excluded = [r for r in records if is_excluded(r)]
    return included, excluded


def load_records(root: Path | str) -> list[dict]:
    """Read every run record (a JSON object with an `arm` key) under `root`."""
    root = Path(root)
    if not root.is_dir():
        raise GraphError(f"{root}: not a directory")
    records = []
    for path in sorted(root.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphError(f"{path}: {exc}") from exc
        if not isinstance(data, dict) or "arm" not in data:
            continue
        errors = validate_record(data)
        if errors:
            raise GraphError(f"{path}: {'; '.join(errors)}")
        records.append(data)
    return records


def arm_fields(rec: dict, engines: dict[str, str] | None = None) -> dict[str, Any]:
    """The recorded fields that describe a run's arm, keyed by dotted path."""
    entry = rec["arm"]["entry"]
    out: dict[str, Any] = {
        "arm.entry": entry,
        "engine": (engines or {}).get(entry, "unknown"),
        "arm.harness": rec["arm"]["harness"],
    }
    for section in ("versions", "settings"):
        for name, value in rec[section].items():
            out[f"{section}.{name}"] = value
    return out


def _same(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _numeric_leaves(node: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(node, dict):
        for name, value in node.items():
            out.update(_numeric_leaves(value, f"{prefix}{name}."))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out[prefix.rstrip(".")] = float(node)
    return out


def summarise_arms(
    records: list[dict], engines: dict[str, str] | None = None
) -> list[ArmSummary]:
    """Group records by arm, in first-seen order; refuse a field that varies within an arm."""
    arms: dict[tuple[str, str], ArmSummary] = {}
    seen: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in records:
        key = (rec["arm"]["entry"], rec["arm"]["harness"])
        fields = arm_fields(rec, engines)
        if key not in arms:
            arms[key] = ArmSummary(key[0], key[1], fields)
            seen[key] = []
        seen[key].append(fields)
        arms[key].scores.append(float(rec["score"]))
        arms[key].metrics.append(_numeric_leaves(rec.get("metrics") or {}))
    for key, runs in seen.items():
        names = list(dict.fromkeys(n for f in runs for n in f))
        varying = [n for n in names if len({_same(f.get(n)) for f in runs}) > 1]
        if varying:
            raise ComparisonError(
                varying,
                f"arm {key[0]}:{key[1]}: {', '.join(varying)} differ between its runs",
            )
    return list(arms.values())


def check_comparison(arms: list[ArmSummary]) -> str | None:
    """Return the compared variable ("entry", "harness" or None for one arm).

    Raise ComparisonError naming the fields when the arms differ in more than
    one variable, or in any field outside the compared variable.
    """
    if len(arms) < 2:
        return None
    names = list(dict.fromkeys(n for a in arms for n in a.fields))
    differing = [n for n in names if len({_same(a.fields.get(n)) for a in arms}) > 1]
    varied = list(dict.fromkeys(_VARIABLE_OF[n] for n in differing if n in _VARIABLE_OF))
    others = [n for n in differing if n not in _VARIABLE_OF]
    if len(varied) > 1:
        raise ComparisonError(
            differing,
            f"arms differ in more than one variable ({', '.join(varied)}): "
            + ", ".join(differing),
        )
    if others:
        compared = varied[0] if varied else "none"
        raise ComparisonError(
            others,
            f"arms differ outside the compared variable ({compared}): " + ", ".join(others),
        )
    return varied[0] if varied else None


def _fmt(value: Any) -> str:
    if isinstance(value, dict):
        return "(" + ", ".join(f"{k}={v}" for k, v in value.items()) + ")"
    return str(value)


def disclosure(level: str, variable: str | None, arms: list[ArmSummary], excluded: int) -> str:
    """The disclosure text: every recorded arm field and each arm's run count."""
    lines = [
        f"level: {level}; compared variable: {variable or 'none'}; "
        f"excluded runs: {excluded} (invalid or contaminated)"
    ]
    for arm in arms:
        fields = "; ".join(f"{k}={_fmt(v)}" for k, v in arm.fields.items())
        lines.append(f"{arm.entry} / {arm.harness}: {fields}; runs={arm.run_count}")
    return "\n".join(lines) + "\n"


def _slug(*parts: str) -> str:
    return "-".join(re.sub(r"[^A-Za-z0-9._]+", "-", p).strip("-") for p in parts)


def _base_name(level: str, variable: str | None, arms: list[ArmSummary]) -> str:
    first = arms[0]
    if variable == "harness":
        return _slug(level, "by-harness", first.entry)
    if variable == "entry":
        return _slug(level, "by-entry", first.harness)
    return _slug(level, first.entry, first.harness)


def _label(arm: ArmSummary, variable: str | None) -> str:
    if variable == "harness":
        return arm.harness
    if variable == "entry":
        return arm.entry
    return f"{arm.entry}\n{arm.harness}"


def _mean_and_points(ax: Any, labels: list[str], values: list[list[float]]) -> None:
    """A bar at each arm's mean with every run drawn as a point on it."""
    xs = range(len(labels))
    means = [sum(v) / len(v) if v else 0.0 for v in values]
    ax.bar(xs, means, width=0.6, color="#9db4d0", edgecolor="#3a5f8a", zorder=1)
    for x, vals in zip(xs, values):
        n = len(vals)
        offsets = [(i - (n - 1) / 2) * (0.4 / max(n, 1)) for i in range(n)]
        ax.scatter([x + o for o in offsets], vals, s=14, color="#1f2d3d", zorder=2)
    ax.set_xticks(list(xs), labels, fontsize=7)
    ax.grid(axis="y", alpha=0.3)


def _render(path: Path, title: str, text: str, panels: list[tuple[str, list[list[float]]]], labels: list[str]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    wrapped = "\n".join(
        textwrap.fill(line, 150, subsequent_indent="    ") for line in text.splitlines()
    )
    text_h = 0.14 * (wrapped.count("\n") + 1) + 0.3
    ncols = min(3, len(panels))
    nrows = math.ceil(len(panels) / ncols)
    plot_h = 3.0 * nrows
    fig = Figure(figsize=(11, plot_h + text_h + 0.6))
    for i, (name, values) in enumerate(panels):
        ax = fig.add_subplot(nrows, ncols, i + 1)
        _mean_and_points(ax, labels, values)
        ax.set_title(name, fontsize=9)
        if name == "pass rate":
            top = max([1.0] + [v for vals in values for v in vals])
            ax.set_ylim(0, top * 1.05)
            ax.set_ylabel("held-out test pass rate")
    fig.suptitle(title, fontsize=11)
    total_h = plot_h + text_h + 0.6
    fig.subplots_adjust(bottom=(text_h + 0.4) / total_h, top=1 - 0.5 / total_h, hspace=0.5)
    fig.text(0.01, 0.01, wrapped, fontsize=6, family="monospace", va="bottom")
    fig.savefig(path, format="png", dpi=110)


def generate(
    records: list[dict],
    out_dir: Path | str,
    engines: dict[str, str] | None = None,
    arms: list[tuple[str, str]] | None = None,
) -> list[tuple[Path, str]]:
    """Write per level a pass-rate graph and a behaviour-metrics graph.

    `arms` selects (entry, harness) pairs; default is every arm in the
    records. Every comparison is checked before anything is written.
    Return (image path, disclosure text) per graph.
    """
    if arms is not None:
        wanted = set(arms)
        records = [r for r in records if (r["arm"]["entry"], r["arm"]["harness"]) in wanted]
    included, excluded = split_runs(records)
    if arms is not None:
        found = {(r["arm"]["entry"], r["arm"]["harness"]) for r in included}
        missing = [f"{e}:{h}" for e, h in arms if (e, h) not in found]
        if missing:
            raise GraphError(f"no included runs for arm {', '.join(missing)}")
    if not included:
        raise GraphError("no included run records (all invalid, contaminated or none found)")
    plans = []
    for level in LEVELS:
        level_runs = [r for r in included if r["level"] == level]
        if not level_runs:
            continue
        summary = summarise_arms(level_runs, engines)
        variable = check_comparison(summary)
        n_excluded = sum(1 for r in excluded if r["level"] == level)
        plans.append((level, variable, summary, disclosure(level, variable, summary, n_excluded)))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[tuple[Path, str]] = []
    for level, variable, summary, text in plans:
        base = _base_name(level, variable, summary)
        labels = [_label(a, variable) for a in summary]
        compared = f"by {variable}" if variable else "single arm"
        graphs = [("pass-rate", "pass rate", [("pass rate", [a.scores for a in summary])])]
        metric_names = list(dict.fromkeys(n for a in summary for m in a.metrics for n in m))
        if metric_names:
            panels = [(n, [[m[n] for m in a.metrics if n in m] for a in summary]) for n in metric_names]
            graphs.append(("metrics", "behaviour metrics", panels))
        for suffix, what, panels in graphs:
            image = out / f"{base}-{suffix}.png"
            _render(image, f"{level}: {what} {compared}", text, panels, labels)
            image.with_suffix(".txt").write_text(text)
            written.append((image, text))
    return written
