"""The service facade: one protocol naming every artifact operation the CLI
performs, and the local implementation that performs each as an in-process
call on files.

The CLI resolves a ``ProjectService`` and reads or writes project artifacts
only through it, so a project whose files live in the checkout and one held
by a daemon are served by the same commands. The local service is the
in-process half of that contract: every method hands the call to the module
function that already owns the operation, on the checkout root and its
config.
"""

import hashlib
import inspect

import pytest

from specflo import config, daemon
from specflo.daemon import auth
from specflo.errors import SpecfloError
from specflo.service import LocalProjectService, ProjectService
from specflo.service.remote import RemoteProjectService


def operations() -> list[str]:
    """The public operations the protocol names, in declaration order."""
    return [
        name
        for name, value in vars(ProjectService).items()
        if callable(value) and not name.startswith("_")
    ]


class _Recorder:
    """Delegates every call to a service and records which operations ran."""

    def __init__(self, service):
        self._service = service
        self.called: set[str] = set()

    def __getattr__(self, name):
        attr = getattr(self._service, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.called.add(name)
            return attr(*args, **kwargs)

        return call


@pytest.fixture
def local(tmp_path):
    """A local service over a freshly initialised checkout."""
    cfg = config.init_config(tmp_path)
    return LocalProjectService(tmp_path, cfg)


# --- the protocol ----------------------------------------------------------


def _assert_satisfies_the_protocol(service, implementation):
    assert isinstance(service, ProjectService)
    for name in operations():
        expected = inspect.signature(getattr(ProjectService, name))
        actual = inspect.signature(getattr(implementation, name))
        assert actual == expected, f"{name}: {actual} != {expected}"


def test_local_service_satisfies_the_protocol(local):
    _assert_satisfies_the_protocol(local, LocalProjectService)


def test_local_service_protocol_names_every_operation_group():
    named = set(operations())
    for representative in (
        "create_project",
        "advance_project",
        "validate_artifact",
        "complete_artifact",
        "add_decision",
        "add_requirement",
        "add_task",
        "task_brief",
        "add_milestone",
        "add_pool",
        "start_round",
        "close_round",
        "build_checkpoint",
        "write_checkpoint",
        "build_status",
        "show_document",
        "set_section",
    ):
        assert representative in named


# --- the local implementation ---------------------------------------------


def _drive_every_operation(service, projects_root):
    """Drive one project through every operation; ``projects_root`` is where
    the service keeps its project directories and its ledger."""
    svc = _Recorder(service)

    project = svc.create_project("My Thing", summary="One line", execution="linear")
    slug = project.slug
    assert svc.load_project(slug).phase == "brainstorm"
    assert [p.slug for p in svc.list_projects()] == [slug]
    assert svc.has_artifact(slug, "project")
    assert not svc.has_artifact(slug, "brainstorm")

    # brainstorm: decisions, prose, the gate, the phase bump
    path, created = svc.start_brainstorm(slug)
    assert created and path == projects_root / slug / "brainstorm.md"
    assert svc.start_brainstorm(slug) == (path, False)
    decision = svc.add_decision(slug, "Use one facade", rationale="one seam")
    assert decision.text == "Use one facade"
    assert decision.id in svc.show_document(slug, "brainstorm")
    assert any("Out of scope" in issue for issue in svc.validate_artifact(slug, "brainstorm"))
    title = svc.set_section(slug, "brainstorm", "Out of scope / Deferred", "No auth.\n")
    assert title == "Out of scope / Deferred"
    assert "No auth." in svc.show_document(slug, "brainstorm")
    assert svc.validate_artifact(slug, "brainstorm") == []
    svc.complete_artifact(slug, "brainstorm")
    assert svc.advance_project(slug).phase == "spec"

    # spec: requirements
    path, created = svc.start_spec(slug)
    assert created and path.name == "spec.md"
    requirement = svc.add_requirement(
        slug, "Prints help", "a no-arg run exits 0", derives_from=decision.id
    )
    assert requirement.derives_from == decision.id
    svc.set_section(slug, "spec", "In scope", "- the CLI.\n")
    svc.set_section(slug, "spec", "Out of scope", "- the GUI.\n")
    assert svc.validate_artifact(slug, "spec") == []
    svc.complete_artifact(slug, "spec")
    assert svc.advance_project(slug).phase == "plan"

    # plan: milestones, pools, tasks and their reads
    path, created = svc.start_plan(slug)
    assert created and path.name == "plan.md"
    milestone = svc.add_milestone(slug, "Help works", ["help prints"])
    assert milestone.exit_items == ["help prints"]
    assert svc.add_pool(slug, "gpu", 2) == ("gpu", 2)
    assert svc.list_pools(slug) == {"gpu": 2}
    first = svc.add_task(
        slug, "Build help", "help prints", "uv run pytest", [requirement.id],
        files="src/help.py", needs=["gpu"], milestone=milestone.id,
    )
    second = svc.add_task(
        slug, "Polish help", "help is tidy", "uv run pytest", [requirement.id],
        depends_on=[first.id],
    )
    third = svc.add_task(
        slug, "Document help", "help is documented", "uv run pytest", [requirement.id],
        depends_on=[second.id], milestone=milestone.id,
    )
    assert svc.set_milestone(slug, second.id, milestone.id).milestone == milestone.id
    assert svc.edit_task(slug, second.id, scope="Small") == (second.id, ["scope"])
    note = svc.add_note(slug, first.id, "why this shape", label="Design")
    assert note["label"] == "Design"
    assert [t.id for t in svc.list_tasks(slug)] == [first.id, second.id, third.id]
    assert svc.plan_progress(slug)["total"] == 3
    assert svc.frontier(slug)["pools"]["gpu"]["size"] == 2
    graph = svc.execution_graph(slug)
    assert [m.id for m in graph["milestones"]] == [milestone.id]
    assert svc.task_brief(slug)["task"]["id"] == first.id
    assert svc.current_task_id(slug) == first.id
    assert svc.milestone_progress(slug)["current"] == milestone.id
    assert svc.milestone_detail(slug, milestone.id)["total"] == 3
    assert svc.plan_warnings(slug) == []
    assert svc.resolution_notes(slug) == []
    assert svc.validate_artifact(slug, "plan") == []
    svc.complete_artifact(slug, "plan")
    assert svc.advance_project(slug).phase == "execute"

    # execute: task transitions and supersession
    assert svc.start_task(slug, first.id).progress == "in_progress"
    assert svc.block_task(slug, first.id, reason="waiting").blocked == "waiting"
    assert svc.reopen_task(slug, first.id, note="unblocked").progress == "pending"
    svc.start_task(slug, first.id)
    assert svc.done_task(slug, first.id, note="shipped").progress == "done"
    replacement = svc.add_task(
        slug, "Polish help again", "help is tidy", "uv run pytest", [requirement.id],
        depends_on=[first.id], supersedes=second.id, milestone=milestone.id,
    )
    assert svc.active_dependents(slug, second.id) == [third.id]
    assert svc.rewire_dependency(slug, second.id, replacement.id) == [third.id]
    for task_id in (replacement.id, third.id):
        svc.start_task(slug, task_id)
        svc.done_task(slug, task_id)
    assert svc.task_brief(slug)["task"] is None

    # review: the execute gate reads the round
    round_path, created = svc.start_round(slug)
    assert created and round_path.name == "review-1.md"
    assert svc.validate_artifact(slug, "execute") != []
    assert svc.close_round(slug, "ready-to-merge") == round_path
    assert svc.validate_artifact(slug, "execute") == []

    # checkpoint and status are derived from the artifacts
    assert svc.build_checkpoint(slug)["phase"] == "execute"
    checkpoint = svc.write_checkpoint(slug)
    assert checkpoint.name == "checkpoint.md"
    assert svc.has_artifact(slug, "checkpoint")
    assert svc.show_document(slug, "checkpoint") == checkpoint.read_text()
    status = svc.build_status(slug)
    assert status["active_project"] == slug and status["phase"] == "execute"

    # the rest of the project lifecycle
    assert svc.set_summary(slug, "Ships help").summary == "Ships help"
    assert svc.set_execution(slug, "fan-out") == ("fan-out", True)
    assert svc.set_execution(slug, "fan-out") == ("fan-out", False)
    assert svc.shelve_project(slug, reason="later").shelved_reason == "later"
    assert svc.resume_project(slug).status == "active"
    assert svc.reopen_project(slug).phase == "plan"
    assert svc.reopen_project(slug, "brainstorm").phase == "brainstorm"
    assert svc.complete_project(slug).status == "complete"
    assert [p.name for p in svc.stamp_banners(slug)] == ["brainstorm.md", "spec.md"]

    # the ledger over every project
    assert not svc.index_exists()
    index = svc.write_index()
    assert index == projects_root / "specflo-index.md"
    assert svc.index_exists()
    assert "Ships help" in index.read_text()
    assert svc.index_rule_line()

    # the project as a set of files, and a second project made from them
    files = svc.export_project(slug)
    assert set(files) >= {"project.md", "brainstorm.md", "spec.md", "plan.md", "checkpoint.md", "review-1.md"}
    assert svc.import_project("copy", files) == {
        name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()
    }
    assert svc.show_document("copy", "spec") == files["spec.md"]

    assert svc.called == set(operations())


def test_local_service_drives_a_project_through_every_operation(local, tmp_path):
    _drive_every_operation(local, tmp_path / "docs" / "projects")


def test_local_service_raises_the_module_errors_unchanged(local):
    with pytest.raises(SpecfloError, match="No project 'nope'"):
        local.load_project("nope")
    with pytest.raises(SpecfloError, match="brainstorm, execute, plan, spec"):
        local.validate_artifact("nope", "notes")
    with pytest.raises(SpecfloError, match="brainstorm, spec, plan"):
        local.complete_artifact("nope", "execute")
    with pytest.raises(SpecfloError, match="No plan yet"):
        local.list_tasks("nope")


def test_local_service_keeps_every_write_under_its_root(local, tmp_path):
    project = local.create_project("Thing")
    local.start_brainstorm(project.slug)
    local.write_checkpoint(project.slug)
    local.write_index()

    written = sorted(
        str(p.relative_to(tmp_path))
        for p in (tmp_path / "docs").rglob("*")
        if p.is_file()
    )
    assert written == [
        "docs/projects/specflo-index.md",
        "docs/projects/thing/brainstorm.md",
        "docs/projects/thing/checkpoint.md",
        "docs/projects/thing/project.md",
    ]


# --- the CLI performs artifact I/O only through the facade -----------------

# The artifact modules, by the name the CLI imports each under, and the
# members the CLI may still reach in them: constants, types, and pure
# renderers that never touch a file. Everything else goes through the
# service.
_ARTIFACT_MODULE_ALLOWLIST = {
    "projects": {"slugify", "Project", "LINEAR_EXECUTION", "COMPLETE_STATUS", "SHELVED_STATUS"},
    "brainstorm": {"BRAINSTORM_FILENAME"},
    "spec": {"SPEC_FILENAME"},
    "plan": {"PLAN_FILENAME", "Task", "render_task_brief", "boundary_beat_lines"},
    "review_module": set(),
    "checkpoint": {"render_checkpoint", "hosted_view"},
    "status_view": {"render_status", "hosted_view"},
    "doc_module": {"ARTIFACTS", "PROSE_ARTIFACTS"},
    "index_module": set(),
}

# File primitives a command may not call on anything: an artifact path is
# never in a command's hands, so there is nothing for these to act on.
_FILE_PRIMITIVES = {"open", "write_text", "read_bytes", "write_bytes", "is_file", "exists", "mkdir", "unlink"}


def _cli_tree():
    import ast
    import inspect

    from specflo import cli

    return ast.parse(inspect.getsource(cli)), cli


def _locally_bound(fn) -> set[str]:
    """The names a function binds itself: its parameters and every assignment
    target. A local that shadows a module name is not that module."""
    import ast

    args = fn.args
    bound = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    bound |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    bound |= {
        node.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    return bound


def test_structural_cli_reaches_artifact_modules_only_for_pure_members():
    import ast

    tree, _ = _cli_tree()
    offenders = []
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed = _locally_bound(scope)
            nodes = ast.walk(scope)
        elif scope is tree:
            shadowed = set()
            nodes = [n for n in ast.iter_child_nodes(tree)
                     if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            nodes = [n for top in nodes for n in ast.walk(top)]
        else:
            continue
        for node in nodes:
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            base = node.value.id
            if base in shadowed:
                continue
            allowed = _ARTIFACT_MODULE_ALLOWLIST.get(base)
            if allowed is not None and node.attr not in allowed:
                offenders.append(f"{base}.{node.attr} (line {node.lineno})")
    assert offenders == []


def test_structural_cli_never_touches_a_file_itself():
    import ast

    tree, _ = _cli_tree()
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FILE_PRIMITIVES:
            offenders.append(f".{node.attr} (line {node.lineno})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FILE_PRIMITIVES:
            offenders.append(f"{node.func.id}() (line {node.lineno})")
    assert offenders == []


def test_structural_cli_reads_text_only_from_the_callers_own_file():
    """``section set --file`` reads the caller's input file, never an artifact;
    no other command reads text at all."""
    import ast

    tree, _ = _cli_tree()
    readers = sorted({
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and node.attr == "read_text"
    })
    assert readers == ["section_set"]


def test_structural_cli_obtains_its_service_from_the_one_resolver():
    from conftest import executable_identifiers

    from specflo import cli
    from specflo.service import resolve

    code = executable_identifiers(cli)
    assert "resolve_service" in code
    assert "localprojectservice" not in code
    assert "localprojectservice" in executable_identifiers(resolve)


# --- the remote implementation --------------------------------------------


@pytest.fixture
def daemon_root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def remote(daemon_root):
    """A remote service whose HTTP client runs the daemon in-process."""
    from fastapi.testclient import TestClient

    from specflo.daemon.app import create_app

    token = auth.mint_token(daemon_root, "developer")
    return RemoteProjectService(
        "http://testserver", token, client=TestClient(create_app(daemon_root))
    )


def test_remote_service_satisfies_the_protocol(remote):
    _assert_satisfies_the_protocol(remote, RemoteProjectService)


def test_remote_service_drives_a_project_through_every_operation(remote, daemon_root):
    _drive_every_operation(remote, daemon_root / daemon.PROJECTS_DIRNAME)


def test_remote_service_raises_the_daemons_refusals_as_specflo_errors(remote):
    with pytest.raises(SpecfloError, match="No project 'nope'"):
        remote.load_project("nope")
    with pytest.raises(SpecfloError, match="brainstorm, execute, plan, spec"):
        remote.validate_artifact("nope", "notes")
    with pytest.raises(TypeError):
        remote.add_decision("nope")


def test_remote_service_reports_a_refused_token_naming_the_remote(daemon_root):
    from fastapi.testclient import TestClient

    from specflo.daemon.app import create_app

    service = RemoteProjectService(
        "http://testserver", "not-a-token", client=TestClient(create_app(daemon_root))
    )

    with pytest.raises(SpecfloError, match="http://testserver.*token"):
        service.list_projects()


def test_remote_service_reports_an_unreachable_daemon_naming_the_remote():
    service = RemoteProjectService("http://127.0.0.1:9", "token", timeout=0.5)

    with pytest.raises(SpecfloError, match="http://127.0.0.1:9"):
        service.list_projects()


def test_remote_service_sends_the_bearer_token_on_every_request(daemon_root):
    from fastapi.testclient import TestClient

    from specflo.daemon.app import create_app

    token = auth.mint_token(daemon_root, "requester")
    service = RemoteProjectService(
        "http://testserver", token, client=TestClient(create_app(daemon_root))
    )

    assert service.list_projects() == []
    assert service.client.headers["Authorization"] == f"Bearer {token}"
