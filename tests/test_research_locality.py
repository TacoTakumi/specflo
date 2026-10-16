"""Every research command stays in the project folder.

An end-to-end run of every ``specflo research`` command, and of the project
verbs a research project uses, on a fixture checkout. The checkout is
snapshotted before and after each command (every file, dotfiles and
``.specflo/`` included, by sha256), and each path that changed or appeared
is under the active project's folder or is one of specflo's own files: the
projects index and the files under ``.specflo/``. Nothing is removed, in the
checkout or beside the input files. A command that only reads changes
nothing but, at most, a lock file under ``.specflo/locks/``. A spin-off may
also create its new project's folder. The same run passes with ``awiki``
absent from PATH, and the research package names no wiki and imports no
network module.
"""

import ast
import hashlib
import io
import shutil
import tokenize
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, index, research, research_cli
from specflo.cli import app

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "research"
PDF = FIXTURES / "three-pages.pdf"
DOCX = FIXTURES / "headings.docx"
# The markdown source holds a figure of each kind the lint looks for (a
# number, a date, a quoted string), so a finding that cites it is grounded
# and the lint exits 0. The first page of the PDF reads "rent rose 4 percent".
NOTES = 'Rent rose 4% in 2025 and the "renewal clause" runs five years.\n'
INPUTS = {
    "notes.md": NOTES,
    "brief.txt": "A plain brief: the tenant pays the taxes.\n",
    "interview.md": "Ana: renewals are the hard part.\n",
    "repo.md": "The README of the lease tool.\n",
}
PAGE = "The act, as published.\n"
BROKER = "A broker with twenty years in commercial real estate."
SPECFLO_DIR = ".specflo/"
LOCKS = ".specflo/locks/"
WRITES, READS = True, False

# The research package and its CLI module: what the source scan reads.
RESEARCH_SOURCES = sorted([*Path(research.__file__).parent.glob("*.py"), Path(research_cli.__file__)])
NETWORK_MODULES = ("socket", "urllib", "http.client", "httpx", "requests")


# --- snapshots ---------------------------------------------------------------------


def _snapshot(directory):
    """Every file under ``directory`` (dotfiles included) by relative posix path -> sha256."""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _diff(before, after):
    """The changed, added and removed paths between two snapshots."""
    changed = {path for path in before.keys() & after.keys() if before[path] != after[path]}
    return changed, set(after) - set(before), set(before) - set(after)


def _under(path, allowed):
    """True when ``path`` is one of the allowed files or lies under an allowed folder."""
    return any(path == each or (each.endswith("/") and path.startswith(each)) for each in allowed)


def _assert_within(before, after, allowed, label):
    """Every changed or added path is in ``allowed``; nothing is removed."""
    changed, added, removed = _diff(before, after)
    assert not removed, (label, sorted(removed))
    outside = sorted(path for path in changed | added if not _under(path, allowed))
    assert not outside, (label, outside)


def _assert_reads_only(before, after, label):
    """Nothing changed or was removed; at most a lock file appeared."""
    changed, added, removed = _diff(before, after)
    assert not changed, (label, sorted(changed))
    assert not removed, (label, sorted(removed))
    assert all(path.startswith(LOCKS) for path in added), (label, sorted(added))


# --- the runner and the checkout ------------------------------------------------------


def _run(args, code, stdin=None):
    result = runner.invoke(app, args, input=stdin)
    assert result.exit_code == code, (args, result.exit_code, result.output)
    assert "Traceback" not in result.output, (args, result.output)
    return result


def _checkout(path, monkeypatch):
    path.mkdir()
    config.init_config(path)
    monkeypatch.chdir(path)
    return path


def _folder(root, cfg, slug):
    """A project's folder as an allowed prefix, relative to the checkout root."""
    return (Path(cfg.projects_dir) / slug).as_posix() + "/"


def _own_files(root, cfg):
    """specflo's own files: everything under .specflo/ and the projects index."""
    return (SPECFLO_DIR, index.index_path(root, cfg).relative_to(root).as_posix())


def _inputs(tmp_path):
    """The files the run ingests, written beside the checkout; their paths by name."""
    for name, text in INPUTS.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    shutil.copyfile(PDF, tmp_path / PDF.name)
    shutil.copyfile(DOCX, tmp_path / DOCX.name)
    return {path.name: str(path) for path in tmp_path.iterdir() if path.is_file()}


def _steps(inputs):
    """Every command of the run on the research project, in order.

    Each step is (args, stdin, expected exit code, writes). The sources come
    in as S-01 (notes), S-02 (brief), S-03 (the PDF), S-04 (the Word file),
    S-05 to S-07 (interview, web, repo); the findings cite S-01 and page 1 of
    S-03 so the lint passes. A list with --open on a ledger other than the
    questions is refused, and the refusal writes nothing either.
    """
    url = "https://example.org/act"
    return [
        # The index exists from here on, so each later refresh of it is seen.
        (["index"], None, 0, WRITES),
        (["research", "ingest", inputs["notes.md"]], None, 0, WRITES),
        (["research", "ingest", inputs["brief.txt"]], None, 0, WRITES),
        (["research", "ingest", inputs[PDF.name]], None, 0, WRITES),
        (["research", "ingest", inputs[DOCX.name]], None, 0, WRITES),
        (["research", "source", "add", "--kind", "interview", "--title", "Ana on renewals",
          "--file", inputs["interview.md"]], None, 0, WRITES),
        (["research", "source", "add", "--kind", "web", "--title", "The act online",
          "--stdin", "--url", url], PAGE, 0, WRITES),
        (["research", "source", "add", "--kind", "repo", "--title", "The lease tool",
          "--file", inputs["repo.md"], "--url", "https://example.org/lease-tool.git"],
         None, 0, WRITES),
        (["research", "user", "set", "--text", BROKER], None, 0, WRITES),
        (["research", "destination", "set", "build", "--reason", "the user wants a tool"],
         None, 0, WRITES),
        (["research", "destination", "set", "inform", "--reason", "a report is enough for now"],
         None, 0, WRITES),
        (["research", "question", "add", "--text", "Which market first?", "--by", "user",
          "--option", "Toronto", "--option", "Calgary"], None, 0, WRITES),
        (["research", "question", "add", "--text", "Which database?", "--by", "developer",
          "--default", "sqlite", "--why", "the tool needs one"], None, 0, WRITES),
        (["research", "question", "resolve", "Q-01", "--resolution", "Toronto first.",
          "--by", "Ana"], None, 0, WRITES),
        (["research", "finding", "add", "--text", "Rent rose 4 percent.", "--kind", "fact",
          "--status", "supported", "--cites", "S-03:p1"], None, 0, WRITES),
        (["research", "finding", "add", "--text", "Vacancy is falling.", "--kind", "inference",
          "--status", "unsure"], None, 0, WRITES),
        (["research", "finding", "add", "--text", "Rent rose 4% in 2025.", "--kind", "fact",
          "--status", "supported", "--cites", "S-01"], None, 0, WRITES),
        (["research", "finding", "add", "--text", NOTES.strip(), "--kind", "fact",
          "--status", "supported", "--cites", "S-01", "--supersedes", "RF-03"], None, 0, WRITES),
        (["research", "term", "set", "--term", "Cap rate", "--definition",
          "net income over price"], None, 0, WRITES),
        (["research", "term", "set", "--term", "Cap rate", "--definition",
          "net operating income over the price", "--avoid", "yield"], None, 0, WRITES),
        (["research", "session", "close", "--summary", "Read the act",
          "--next", "Ask about renewals"], None, 0, WRITES),
        (["decision", "add", "--text", "Keep the glossary", "--rationale", "the expert's words"],
         None, 0, WRITES),
        (["research", "list", "sources"], None, 0, READS),
        (["research", "list", "sources", "--json"], None, 0, READS),
        (["research", "list", "sources", "--open"], None, 1, READS),
        (["research", "list", "questions"], None, 0, READS),
        (["research", "list", "questions", "--json"], None, 0, READS),
        (["research", "list", "questions", "--open"], None, 0, READS),
        (["research", "list", "questions", "--open", "--json"], None, 0, READS),
        (["research", "list", "findings"], None, 0, READS),
        (["research", "list", "findings", "--json"], None, 0, READS),
        (["research", "list", "findings", "--open"], None, 1, READS),
        (["research", "list", "terms"], None, 0, READS),
        (["research", "list", "terms", "--json"], None, 0, READS),
        (["research", "list", "terms", "--open"], None, 1, READS),
        (["research", "lint"], None, 0, READS),
        (["research", "recap"], None, 0, READS),
        (["research", "recap", "--json"], None, 0, READS),
        (["research", "ask-dev"], None, 0, READS),
        (["doc", "show", "research"], None, 0, READS),
        (["validate", "research"], None, 0, READS),
        (["checkpoint"], None, 0, WRITES),
        (["status"], None, 0, READS),
        (["status", "--json"], None, 0, READS),
    ]


# --- awiki on PATH or not ------------------------------------------------------------


@pytest.fixture(params=["as-is", "no-awiki"])
def wiki(request, tmp_path, monkeypatch):
    """PATH as the test runner has it, or an empty directory with awiki nowhere to be found."""
    if request.param == "no-awiki":
        empty = tmp_path / "bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        real_which = shutil.which

        def which(cmd, *args, **kwargs):
            return None if cmd == "awiki" else real_which(cmd, *args, **kwargs)

        monkeypatch.setattr(shutil, "which", which)
        assert shutil.which("awiki") is None
    return request.param


# --- the run ----------------------------------------------------------------------------


def test_every_research_command_writes_only_in_the_project_folder(tmp_path, monkeypatch, wiki):
    inputs = _inputs(tmp_path)
    root = _checkout(tmp_path / "checkout", monkeypatch)
    cfg = config.load_config(root)
    own = _own_files(root, cfg)
    thing = _folder(root, cfg, "thing")
    beside = {path: digest for path, digest in _snapshot(tmp_path).items()
              if not path.startswith("checkout/")}

    before = _snapshot(root)
    _run(["new", "Thing", "--level", "research"], 0)
    _assert_within(before, _snapshot(root), (thing, *own), "new")

    for args, stdin, code, writes in _steps(inputs):
        before = _snapshot(root)
        _run(args, code, stdin)
        after = _snapshot(root)
        if writes:
            _assert_within(before, after, (thing, *own), args)
        else:
            _assert_reads_only(before, after, args)
    assert (root / thing / "research" / "sources" / "S-07").is_dir()
    assert (root / thing / "checkpoint.md").is_file()

    # The spin-off creates the new project's folder, logs in the research
    # project and moves the active pointer; it touches nothing else.
    lease = _folder(root, cfg, "lease-tool")
    before = _snapshot(root)
    _run(["research", "spinoff", "Lease tool", "--level", "fast", "--use", "RF-01,Q-02"], 0)
    after = _snapshot(root)
    _assert_within(before, after, (thing, lease, *own), "spinoff")
    changed, added, _ = _diff(before, after)
    assert (root / lease / "handoff.md").is_file()
    assert all(path.startswith((thing, lease)) or _under(path, own) for path in changed | added)
    assert any(path.startswith(lease) for path in added)
    assert config.load_config(root).active_project == "lease-tool"

    before = _snapshot(root)
    _run(["switch", "thing"], 0)
    _assert_within(before, _snapshot(root), (thing, *own), "switch")
    assert config.load_config(root).active_project == "thing"
    before = _snapshot(root)
    _run(["research", "recap"], 0)
    _assert_reads_only(before, _snapshot(root), "recap after the switch")

    # A second research project moved up to full changes only its own folder.
    other = _folder(root, cfg, "other")
    before = _snapshot(root)
    _run(["new", "Other", "--level", "research"], 0)
    _assert_within(before, _snapshot(root), (other, *own), "new other")
    before = _snapshot(root)
    _run(["level", "full"], 0)
    after = _snapshot(root)
    _assert_within(before, after, (other, *own), "level full")
    assert (root / other / "brainstorm.md").is_file()
    assert not any(path.startswith((thing, lease)) for path in set().union(*_diff(before, after)))

    # The input files beside the checkout are as they were, and nothing
    # appeared beside them.
    after_beside = {path: digest for path, digest in _snapshot(tmp_path).items()
                    if not path.startswith("checkout/")}
    assert after_beside == beside


# --- the source scan ----------------------------------------------------------------------


def _without_comments(source):
    """The source's tokens, comments left out, one per line."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return "\n".join(token.string for token in tokens if token.type != tokenize.COMMENT)


def _imported_modules(source):
    """Every module name the source imports, dotted names and from-imports included."""
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _is_network(name):
    return any(name == module or name.startswith(module + ".") for module in NETWORK_MODULES)


def _network_imports(source):
    return sorted({name for name in _imported_modules(source) if _is_network(name)})


def test_the_scan_helpers_see_what_they_look_for():
    assert "awiki" not in _without_comments("x = 1  # awiki is only in this comment\n")
    assert "awiki" in _without_comments('x = "awiki"\n')
    assert "awiki" in _without_comments("awiki = 1\n")
    planted = "import urllib.request\nfrom http import client\nimport httpx, json\nfrom . import x\n"
    assert _network_imports(planted) == ["http.client", "httpx", "urllib.request"]
    assert _network_imports("import json\nfrom pathlib import Path\n") == []


def test_the_research_package_names_no_wiki_and_imports_no_network_module():
    names = {path.name for path in RESEARCH_SOURCES}
    assert {"__init__.py", "ledger.py", "spinoff.py", "research_cli.py"} <= names
    for path in RESEARCH_SOURCES:
        source = path.read_text(encoding="utf-8")
        assert "awiki" not in _without_comments(source).lower(), path.name
        assert _network_imports(source) == [], path.name
