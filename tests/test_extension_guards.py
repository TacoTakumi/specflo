"""Structural guards over the shipped pi extension source (T-08).

Four of the extension's requirements are *absences* - things the source must
never contain - and an absence has no runtime test that can prove it. This
module scans the shipped tree instead and fails on any of:

- ``registerTool``          - no model-callable tool (REQ-15)
- ``on("tool_call")``       - not a gatekeeper, blocks nothing (REQ-03)
- ``appendEntry``           - no durable state (REQ-02)
- ``clearthen``             - the clear is ours, not pi-clearthen's (REQ-14)
- filesystem access         - all specflo state comes from the CLI (REQ-01)

The scan runs over comment-stripped source. A raw substring scan cannot express
"the code must not *do* this": it also flags the comments and docstrings that
describe the very thing being ruled out, so documenting an invariant would break
its own guard. String literals are deliberately *kept* - ``on("tool_call")`` and
a ``".specflo"`` path are both string-carried - so only comments come out.

Every rule is proved in both directions: the current source is clean, and a
deliberately injected violation of each kind is caught.
"""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from specflo import extension_install


# --- comment stripping ------------------------------------------------------


def strip_comments(text: str) -> str:
    """TypeScript source with comments removed and string literals intact.

    String-aware, so a ``//`` inside a URL literal or a ``/*`` inside a message
    is not mistaken for a comment. Comments are replaced by a space rather than
    deleted, so tokens on either side never fuse into a new identifier.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # escape: copy the pair verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --- the guard --------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    kind: str
    file: str
    detail: str


#: Source-level rules, each a (kind, compiled pattern) over stripped TS source.
_SOURCE_RULES = (
    # REQ-15: no model-callable tool.
    ("register-tool", re.compile(r"\bregisterTool\s*\(")),
    # REQ-03: no tool_call handler -- the extension gates nothing.
    ("tool-call-handler", re.compile(r"""\bon\s*\(\s*["'`]tool_call["'`]""")),
    # REQ-02: no durable state written into the session log.
    ("append-entry", re.compile(r"\bappendEntry\s*\(")),
    # REQ-14: the clear is performed by us, never delegated to pi-clearthen.
    ("clearthen", re.compile(r"clearthen", re.IGNORECASE)),
    # REQ-01/REQ-02: no filesystem capability at all -- neither read nor write.
    (
        "fs-access",
        re.compile(
            r"""from\s*["'`](?:node:)?fs(?:/promises)?["'`]"""
            r"""|require\s*\(\s*["'`](?:node:)?fs(?:/promises)?["'`]"""
            r"""|\b(?:readFile|writeFile|appendFile|openSync|readdir|mkdir|rmdir|unlink|existsSync|statSync)\w*\s*\("""
        ),
    ),
    # REQ-01: no specflo state path is named, let alone opened.
    (
        "specflo-state-path",
        re.compile(
            r"""\.specflo|auto-run\.json|config\.yaml"""
            r"""|(?:brainstorm|spec|plan|project|checkpoint)\.md"""
        ),
    ),
)

#: package.json dependency fields scanned for a pi-clearthen edge (REQ-14).
_DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "bundledDependencies",
)


#: Suffixes scanned as extension source.
_SOURCE_SUFFIXES = {".ts", ".js", ".mts", ".mjs"}


def loadable_files(source: Path) -> list[Path]:
    """The files pi actually loads: package.json plus everything under ``src/``.

    Scoped to what runs inside pi rather than the whole tree, because the tree
    also carries ``test/`` -- an end-to-end harness that spawns processes, writes
    temp files and names specflo paths on purpose. pi never loads it: discovery
    resolves the package through ``package.json``'s ``pi.extensions``, which
    points into ``src/`` only. Guarding code pi never runs would say nothing
    about the extension and would forbid the harness from testing it.
    """
    files = [p for p in (source / "src").rglob("*") if p.is_file()]
    manifest = source / "package.json"
    if manifest.is_file():
        files.append(manifest)
    return sorted(files)


def scan_extension(source: Path) -> list[Violation]:
    """Every structural violation in the extension source at ``source``."""
    violations: list[Violation] = []
    for file in loadable_files(source):
        relpath = file.relative_to(source).as_posix()
        if file.suffix in _SOURCE_SUFFIXES:
            stripped = strip_comments(file.read_text())
            for kind, pattern in _SOURCE_RULES:
                match = pattern.search(stripped)
                if match:
                    violations.append(Violation(kind, relpath, match.group(0)))
        elif relpath == "package.json":
            manifest = json.loads(file.read_text())
            for field in _DEPENDENCY_FIELDS:
                declared = manifest.get(field) or {}
                names = declared if isinstance(declared, (dict, list)) else []
                for name in names:
                    if "clearthen" in str(name).lower():
                        violations.append(
                            Violation("clearthen-dependency", relpath, f"{field}.{name}")
                        )
    return violations


# --- the shipped source is clean --------------------------------------------


def test_shipped_extension_source_has_no_structural_violations():
    assert scan_extension(extension_install.extension_source()) == []


def test_scan_covers_every_entry_point_package_json_declares():
    # A guard that scanned nothing would also report zero violations. Assert
    # every entry point pi loads is a file the scan actually reads.
    source = extension_install.extension_source()
    manifest = json.loads((source / "package.json").read_text())
    scanned = set(loadable_files(source))
    for entry in manifest["pi"]["extensions"]:
        target = (source / entry).resolve()
        assert target.is_file()
        assert target.suffix in _SOURCE_SUFFIXES
        assert target in scanned, f"entry point {entry} is outside the scan"


def test_scan_skips_the_end_to_end_test_harness(tmp_path):
    # The harness spawns processes and names specflo paths on purpose; pi never
    # loads it, so it must not be scanned -- and must not be silently in scope
    # either, which this pins by putting a violation there and expecting none.
    source = tmp_path / "extension"
    shutil.copytree(extension_install.extension_source(), source)
    (source / "test").mkdir(exist_ok=True)
    (source / "test" / "harness.ts").write_text(
        'import { readFileSync } from "node:fs";\nconst p = ".specflo/config.yaml";\n'
    )
    assert scan_extension(source) == []


# --- each violation kind is caught ------------------------------------------


def _append_to_index(source: Path, code: str) -> None:
    index = source / "src" / "index.ts"
    index.write_text(index.read_text() + code)


def _add_dependency(source: Path, name: str) -> None:
    manifest_path = source / "package.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dependencies"] = {name: "^0.1.0"}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


VIOLATIONS = [
    (
        "register-tool",
        lambda s: _append_to_index(s, '\npi.registerTool({ name: "x" });\n'),
    ),
    (
        "tool-call-handler",
        lambda s: _append_to_index(s, '\npi.on("tool_call", async () => ({}));\n'),
    ),
    (
        "append-entry",
        lambda s: _append_to_index(s, '\npi.appendEntry({ kind: "note" });\n'),
    ),
    (
        "clearthen",
        lambda s: _append_to_index(s, '\npi.sendUserMessage("/clearthen go");\n'),
    ),
    (
        "fs-access",
        lambda s: _append_to_index(
            s, '\nimport { readFileSync } from "node:fs";\nreadFileSync("x");\n'
        ),
    ),
    (
        "specflo-state-path",
        lambda s: _append_to_index(s, '\nconst p = ".specflo/config.yaml";\n'),
    ),
    ("clearthen-dependency", lambda s: _add_dependency(s, "pi-clearthen")),
]


@pytest.fixture
def mutable_source(tmp_path):
    """A writable copy of the shipped extension tree."""
    copy = tmp_path / "extension"
    shutil.copytree(extension_install.extension_source(), copy)
    return copy


@pytest.mark.parametrize("kind,inject", VIOLATIONS, ids=[k for k, _ in VIOLATIONS])
def test_injected_violation_is_caught(mutable_source, kind, inject):
    assert scan_extension(mutable_source) == []  # clean before the injection
    inject(mutable_source)
    found = {v.kind for v in scan_extension(mutable_source)}
    assert kind in found, f"guard missed an injected {kind} violation"


# --- the stripper itself ----------------------------------------------------


def test_comments_describing_a_forbidden_construct_do_not_trip_the_guard(
    mutable_source,
):
    # The whole point of stripping: an invariant may be documented in the source
    # it constrains without breaking its own guard.
    _append_to_index(
        mutable_source,
        "\n// This extension never calls pi.registerTool or reads .specflo.\n"
        "/* It also registers no on(\"tool_call\") handler and needs no clearthen. */\n",
    )
    assert scan_extension(mutable_source) == []


def test_stripper_keeps_string_literals_that_look_like_comments():
    text = 'const u = "https://example.com/a"; // trailing\nconst b = `/* not a comment */`;\n'
    stripped = strip_comments(text)
    assert '"https://example.com/a"' in stripped
    assert "`/* not a comment */`" in stripped
    assert "trailing" not in stripped


def test_stripper_does_not_fuse_tokens_across_a_removed_comment():
    assert "ab" not in strip_comments("a/* gap */b")
