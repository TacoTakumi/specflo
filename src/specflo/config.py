"""Loading, saving, and scaffolding specflo's per-repo config.

The config lives at ``<root>/.specflo/config.yaml``. Commands locate it by
walking up from the current directory (like git finds ``.git``), so specflo
works from anywhere inside a project tree.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, make_dataclass
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from .errors import SpecfloError

CONFIG_DIRNAME = ".specflo"
CONFIG_FILENAME = "config.yaml"
DEFAULT_PROJECTS_DIR = "docs/projects"


# Autonomy levels for `specflo auto` (REQ-08), defined here rather than in `auto`
# so the registry's validator and `auto` read the same tuple; `auto` re-exports it.
AUTONOMY_LEVELS = ("safe", "autonomous", "yolo")
DEFAULT_AUTONOMY = "safe"
# Default iteration/step cap for `specflo auto` (REQ-14): a runaway backstop on
# the unattended pass loop. Single source of truth for the config-field default,
# `resolve_max_passes`, and the CLI help/docs.
DEFAULT_MAX_PASSES = 50
# Default percent of the model context window at which the pi extension *arms*
# its clear-and-continue trigger (pi-extension REQ-28). Arming is not firing: the
# next specflo seam fires it, so the effective clear point is this percent plus
# one task's worth of context. pi auto-compacts near 92 percent of the window and
# compaction disarms the extension, so arming late defeats the mechanism
# entirely; arming early costs one bounded reseed. Hence 25 (D-01). A percent,
# never an absolute token count, so it holds across window sizes.
DEFAULT_CONTEXT_THRESHOLD_PERCENT = 25
# The usable percent range. 100 is allowed (arm only at a full window, in effect
# disabling the trigger); 0 and below would arm every turn from the start.
CONTEXT_THRESHOLD_RANGE = (1, 100)


def _is_slug(raw: object) -> bool:
    return isinstance(raw, str) and bool(raw.strip())


def _is_count(raw: object, low: int, high: int | None = None) -> bool:
    """A plain positive int in range. ``bool`` is rejected explicitly: it
    subclasses ``int``, so ``True`` would otherwise read as the number 1."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < low:
        return False
    return high is None or raw <= high


@dataclass(frozen=True)
class ConfigField:
    """One config key, described once.

    Every fact about a key lives here - its name, type, shipped default, the
    one-line description comment written above it in the file, and the validator
    deciding whether a value found in the file is usable (REQ-28). Defining any
    of those anywhere else is a defect, even when the two agree.
    """

    name: str
    type: Any
    default: Any
    description: str
    validate: Callable[[Any], bool]


# The registry (REQ-28). Order is the file layout order and the `config list`
# order (REQ-29); appending an entry here is the whole cost of adding a key.
CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "projects_dir",
        str,
        DEFAULT_PROJECTS_DIR,
        "Where project artifacts live, relative to the repo root.",
        _is_slug,
    ),
    ConfigField(
        "active_project",
        str,
        None,
        "The project every command acts on; set it with `specflo switch`.",
        lambda raw: raw is None or _is_slug(raw),
    ),
    ConfigField(
        "autonomy",
        str,
        DEFAULT_AUTONOMY,
        "Default autonomy level for `specflo auto`: safe, autonomous or yolo.",
        lambda raw: raw in AUTONOMY_LEVELS,
    ),
    ConfigField(
        "auto_max_passes",
        int,
        DEFAULT_MAX_PASSES,
        "Runaway backstop: the most passes one `specflo auto` run may take.",
        lambda raw: _is_count(raw, 1),
    ),
    ConfigField(
        "context_threshold_percent",
        int,
        DEFAULT_CONTEXT_THRESHOLD_PERCENT,
        "Percent of the context window at which the pi extension arms clear-and-continue.",
        lambda raw: _is_count(raw, *CONTEXT_THRESHOLD_RANGE),
    ),
)

FIELDS_BY_NAME = {f.name: f for f in CONFIG_FIELDS}


def _annotation(spec: ConfigField) -> str:
    """The dataclass annotation for ``spec``: its value type, made optional when
    the key ships unset. ``type`` itself stays a real type so a CLI layer can
    coerce a string argument with it."""
    return f"{spec.type.__name__} | None" if spec.default is None else spec.type.__name__


# The resolved-values object. Generated from the registry so a key can never
# exist in one and not the other and no default is restated here (REQ-29), while
# every key stays readable as a plain attribute for existing callers (REQ-30).
# `present_keys` is loader metadata, not a config key: which keys the file
# physically held, so writers and `config list` can tell "set" from "defaulted".
SpecfloConfig = make_dataclass(
    "SpecfloConfig",
    [(f.name, _annotation(f), field(default=f.default)) for f in CONFIG_FIELDS]
    + [("present_keys", "frozenset[str]", field(default=frozenset(), compare=False))],
    module=__name__,
)
SpecfloConfig.__doc__ = "Resolved config values, one attribute per CONFIG_FIELDS entry."


def config_path(root: Path) -> Path:
    return root / CONFIG_DIRNAME / CONFIG_FILENAME


def find_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the first directory holding a specflo config."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        if config_path(directory).is_file():
            return directory
    return None


def display_path(path: Path, root: Path, *, posix: bool = False) -> str:
    """``path`` expressed relative to the repo ``root``, or absolute if outside it.

    Defaults to the OS-native separator for terminal display; pass ``posix=True``
    for stored artifact references that must read the same on every platform
    (e.g. the paths written into ``checkpoint.md``).
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix() if posix else str(path)
    return rel.as_posix() if posix else str(rel)


# Keys already warned about in this process. `specflo auto` loads the config many
# times per invocation, so the warning is emitted once per bad key, not once per
# load (REQ-26).
_warned: set[str] = set()


def reset_warnings() -> None:
    """Forget which keys have been warned about. For tests - one process there
    stands in for many runs."""
    _warned.clear()


def _warn_invalid(spec: ConfigField, raw: Any) -> None:
    """One stderr line naming the key and the value found, at most once per key.

    stderr, never stdout: `status --json` is machine-read every turn and its
    stdout must stay parseable JSON (REQ-26).
    """
    if spec.name in _warned:
        return
    _warned.add(spec.name)
    print(
        f"warning: {CONFIG_FILENAME}: invalid {spec.name} {raw!r}"
        f" - using the default {spec.default!r}",
        file=sys.stderr,
    )


def load_config(root: Path) -> SpecfloConfig:
    """Resolve every registry key against the file on disk.

    Driven entirely by :data:`CONFIG_FIELDS` - no key is named here. A value the
    registry's validator rejects degrades to that key's shipped default and warns
    rather than raising: a hand-edited config must not break every command that
    loads it (and `status --json` is polled every turn by the pi extension).
    """
    path = config_path(root)
    if not path.is_file():
        raise SpecfloError(
            f"No specflo project here ({path} not found). Run `specflo init` first."
        )
    data = yaml.safe_load(path.read_text()) or {}
    values = {}
    for spec in CONFIG_FIELDS:
        raw = data.get(spec.name, spec.default)
        if spec.validate(raw):
            values[spec.name] = raw
        else:
            _warn_invalid(spec, raw)
            values[spec.name] = spec.default
    return SpecfloConfig(present_keys=frozenset(data), **values)


# The write path's parser. ruamel's round-trip mode carries comments, key order
# and unknown keys through a load/dump cycle, which PyYAML cannot do; reads stay
# on PyYAML's faster `safe_load` (`status --json` is polled every turn), and
# artifact front matter stays on PyYAML entirely (D-13).
_ROUND_TRIP = YAML()
_ROUND_TRIP.preserve_quotes = True
# The emitter folds a scalar past its line width onto a continuation line, which
# would rewrite a long value we never touched. Wide enough that it never folds.
_ROUND_TRIP.width = 4096
# ruamel writes None as a dangling `active_project:`. Spell it `null`: it reads
# better, and the loader parks the comments following a dangling value on the
# document instead of on the key, which the layout pass then has to go and find.
_ROUND_TRIP.representer.add_representer(
    type(None),
    lambda representer, _: representer.represent_scalar("tag:yaml.org,2002:null", "null"),
)


def _load_document(path: Path) -> CommentedMap:
    """The config file as a round-trip document, or an empty one when there is
    nothing to preserve (no file yet, or a file holding no mapping)."""
    if not path.is_file():
        return CommentedMap()
    doc = _ROUND_TRIP.load(path.read_text())
    return doc if isinstance(doc, CommentedMap) else CommentedMap()


# --- the file layout (REQ-03, REQ-04, REQ-05) ---------------------------
# Every registry key appears in the file: live as `key: value` once it is set,
# commented out at its shipped default while it is not, each directly under its
# one-line registry description and separated from the item above by one blank
# line. The file therefore documents itself, and `config set` reads as
# uncommenting a line the user can already see.
#
# ruamel attaches every comment in a flat mapping to exactly one slot: the text
# before the first key, or the text following key N. So the layout is rebuilt
# slot by slot on each write - the lines this module generates are dropped and
# re-placed, everything else stays where the user put it.


def _render_scalar(value: Any) -> str:
    """``value`` as the emitter would write it after a `key:` (empty for None)."""
    buffer = io.StringIO()
    _ROUND_TRIP.dump({"k": value}, buffer)
    return buffer.getvalue().split(":", 1)[1].strip()


def _description_line(spec: ConfigField) -> str:
    return f"# {spec.description}"


def _commented_line(spec: ConfigField) -> str:
    """A key in its unset form: the name and shipped default, commented out."""
    return f"# {spec.name}: {_render_scalar(spec.default)}"


def _is_generated(line: str) -> bool:
    """True for a comment line this module writes - a key's description, or a
    key commented out at some value.

    Such lines are stripped before the layout is rebuilt, which is what keeps
    repeated saves byte-stable. A hand-written comment of the form
    ``# <registry key>: ...`` is indistinguishable from an unset key by design,
    and is absorbed as one.
    """
    if line in {_description_line(spec) for spec in CONFIG_FIELDS}:
        return True
    name, separator, _ = line.lstrip("#").partition(":")
    return bool(separator) and name.strip() in FIELDS_BY_NAME


def _read_slot(token: Any) -> tuple[str, list[str]]:
    """A comment token as ``(end-of-line comment, the lines below it)``."""
    if token is None:
        return "", []
    eol, _, rest = token.value.partition("\n")
    lines = rest.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return eol, lines


def _take_end(doc: CommentedMap) -> list[str]:
    """The tail comments the loader parked on the document rather than on a key,
    removed from where they sit so the layout pass can place them itself.

    A dangling `key:` (a null written with nothing after it) sends everything
    below it here. specflo writes `null` instead, but a hand-edited file may not.
    """
    tokens = getattr(doc.ca, "end", None)
    if not tokens:
        return []
    doc.ca.end = []
    lines = "".join(token.value for token in tokens).split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _tidy(lines: list[str]) -> list[str]:
    """Collapse the blank runs a strip-out leaves behind. One blank line is the
    item separator, so more than one is always debris."""
    tidied: list[str] = []
    for line in lines:
        if line == "" and (not tidied or tidied[-1] == ""):
            continue
        tidied.append(line)
    while tidied and tidied[-1] == "":
        tidied.pop()
    return tidied


def _relayout(doc: CommentedMap, live: set[str]) -> None:
    """Rewrite ``doc``'s comments so every registry key carries its description
    and every unset key appears commented out at its default."""
    keys = list(doc)
    # slots[i] is the text sitting immediately above keys[i]; the last slot is
    # the tail of the file. eols[i] is the end-of-line comment on keys[i].
    eols, slots = [], [_read_pre_document(doc)]
    for key in keys:
        eol, lines = _read_slot(doc.ca.items.get(key, [None] * 4)[2])
        eols.append(eol)
        slots.append(lines)
    slots[-1].extend(_take_end(doc))
    slots = [_tidy([ln for ln in lines if not _is_generated(ln)]) for lines in slots]

    pending: list[str] = []
    for spec in CONFIG_FIELDS:
        block = ["", _description_line(spec)]
        if spec.name in live:
            slots[keys.index(spec.name)].extend(pending + block)
            pending = []
        else:
            pending.extend(block + [_commented_line(spec)])
    slots[-1].extend(pending)

    while slots[0] and slots[0][0] == "":  # nothing precedes the first item
        slots[0].pop(0)
    _set_pre_document(doc, slots[0])
    for key, eol, lines in zip(keys, eols, slots[1:], strict=True):
        text = eol + "\n" + ("\n".join(lines) + "\n" if lines else "")
        slot = doc.ca.items.setdefault(key, [None] * 4)
        slot[2] = CommentToken(text, CommentMark(0), None) if text.strip() else None


def _read_pre_document(doc: CommentedMap) -> list[str]:
    """The lines above the first key. The loader splits them one token per line."""
    tokens = (doc.ca.comment or [None, None])[1] or []
    lines = "".join(token.value for token in tokens).split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _set_pre_document(doc: CommentedMap, lines: list[str]) -> None:
    text = "\n".join(lines) + "\n" if lines else ""
    doc.ca.comment = [None, [CommentToken(text, CommentMark(0), None)]] if lines else None


def save_config(root: Path, cfg: SpecfloConfig) -> None:
    """Write ``cfg`` back, leaving everything specflo does not own untouched.

    The file is never regenerated. It is loaded into a ruamel round-trip
    document, only the registry keys are assigned, and that same document is
    dumped - so a hand-written comment, the key order the user chose, and a key
    the registry has never heard of all survive the write (REQ-07, REQ-08).
    """
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = _load_document(path)
    live = set()
    for spec in CONFIG_FIELDS:
        value = getattr(cfg, spec.name)
        # A key stays commented out at its default until something claims it -
        # the file itself, or the config asking for the write. That keeps a
        # plain project's config free of live auto-* keys (auto-mode REQ-01).
        if spec.name in doc or spec.name in cfg.present_keys or value != spec.default:
            doc[spec.name] = value
            live.add(spec.name)
    _relayout(doc, live)
    buffer = io.StringIO()
    _ROUND_TRIP.dump(doc, buffer)
    text = buffer.getvalue()
    if not doc:
        # A mapping with no live key emits a bare `{}` under the comments; the
        # comments alone are the file.
        text = text.replace("{}\n", "")
    path.write_text(text)


def init_config(
    root: Path, projects_dir: str = DEFAULT_PROJECTS_DIR, force: bool = False
) -> SpecfloConfig:
    path = config_path(root)
    if path.is_file() and not force:
        raise SpecfloError(
            f"Already initialized ({path} exists). Use --force to re-initialize."
        )
    # The keys a new config carries even at their defaults; the rest arrive in
    # the file only once they are set.
    cfg = SpecfloConfig(
        projects_dir=projects_dir,
        active_project=None,
        present_keys=frozenset({"projects_dir", "active_project"}),
    )
    save_config(root, cfg)
    (root / projects_dir).mkdir(parents=True, exist_ok=True)
    return cfg
