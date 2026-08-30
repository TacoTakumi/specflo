"""graph.py: pure functions over active Task and Milestone lists (fan-out-plans REQ-13)."""

from specflo import graph
from specflo.plan import Milestone, Task


def _task(tid, title, deps=(), milestone=None, files=None, needs=(), progress="pending"):
    return Task(
        id=tid, text=title, acceptance="a", verify="v", implements=["REQ-01"],
        depends_on=list(deps), files=files, scope=None, progress=progress,
        status="active", milestone=milestone, needs=list(needs),
    )


def _fixture():
    tasks = [
        _task("T-01", "first", milestone="M-01", files="src/a.py, src/b.py (note)",
              needs=["gpu:3090"], progress="done"),
        _task("T-02", "second", milestone="M-02"),
        _task("T-03", "third", deps=["T-01", "T-02"], milestone="M-01",
              progress="in_progress"),
        _task("T-04", "fourth", deps=["T-03"], milestone="M-02"),
    ]
    milestones = [Milestone("M-01", "Roots", ["a"]), Milestone("M-02", "Leaves", ["b"])]
    return tasks, milestones


def test_waves_group_by_longest_dependency_path():
    tasks, _ = _fixture()
    assert graph.waves(tasks) == [["T-01", "T-02"], ["T-03"], ["T-04"]]


def test_waves_ignore_dependencies_outside_the_given_tasks():
    # A dependency on a task not passed in (e.g. superseded) does not deepen the wave.
    tasks = [_task("T-02", "b", deps=["T-99"]), _task("T-03", "c", deps=["T-02"])]
    assert graph.waves(tasks) == [["T-02"], ["T-03"]]


def test_waves_is_empty_for_no_tasks():
    assert graph.waves([]) == []


def test_edges_list_one_pair_per_depends_on_entry():
    tasks, _ = _fixture()
    assert graph.edges(tasks) == [["T-01", "T-03"], ["T-02", "T-03"], ["T-03", "T-04"]]


def test_mermaid_has_nodes_subgraphs_and_edges():
    tasks, milestones = _fixture()
    text = graph.mermaid(tasks, milestones)
    lines = text.splitlines()
    assert lines[0] == "```mermaid" and lines[1] == "graph LR" and lines[-1] == "```"
    node_lines = [l for l in lines if '["T-0' in l]
    assert len(node_lines) == 4
    for tid, title in (("T-01", "first"), ("T-02", "second"),
                       ("T-03", "third"), ("T-04", "fourth")):
        assert any(f'["{tid} {title}"]' in l for l in node_lines)
    subgraph_lines = [l for l in lines if l.strip().startswith("subgraph ")]
    assert len(subgraph_lines) == 2
    assert "M-01" in subgraph_lines[0] and "M-02" in subgraph_lines[1]
    assert lines.count("  end") == 2
    edge_lines = [l for l in lines if "-->" in l]
    assert edge_lines == ["  T01 --> T03", "  T02 --> T03", "  T03 --> T04"]


def test_mermaid_places_milestone_tasks_inside_their_subgraph():
    tasks, milestones = _fixture()
    lines = graph.mermaid(tasks, milestones).splitlines()
    m01 = next(i for i, l in enumerate(lines) if "subgraph" in l and "M-01" in l)
    m01_end = next(i for i in range(m01, len(lines)) if lines[i] == "  end")
    inside = "\n".join(lines[m01 + 1:m01_end])
    assert '"T-01 first"' in inside and '"T-03 third"' in inside
    assert "T-02" not in inside and "T-04" not in inside


def test_mermaid_without_milestones_has_top_level_nodes_only():
    tasks, _ = _fixture()
    lines = graph.mermaid(tasks, []).splitlines()
    assert not any("subgraph" in l for l in lines)
    assert len([l for l in lines if '["T-0' in l]) == 4


def test_mermaid_escapes_double_quotes_in_titles():
    text = graph.mermaid([_task("T-01", 'say "hi"')], [])
    assert '"T-01 say #quot;hi#quot;"' in text


def test_payload_carries_waves_tasks_and_edges():
    tasks, milestones = _fixture()
    p = graph.payload(tasks, milestones)
    assert set(p) == {"waves", "tasks", "edges"}
    assert p["waves"] == [["T-01", "T-02"], ["T-03"], ["T-04"]]
    assert p["edges"] == [["T-01", "T-03"], ["T-02", "T-03"], ["T-03", "T-04"]]
    assert p["tasks"][0] == {
        "id": "T-01", "text": "first", "progress": "done",
        "files": ["src/a.py", "src/b.py"], "needs": ["gpu:3090"], "milestone": "M-01",
    }
    assert p["tasks"][1] == {
        "id": "T-02", "text": "second", "progress": "pending",
        "files": [], "needs": [], "milestone": "M-02",
    }
    assert [t["id"] for t in p["tasks"]] == ["T-01", "T-02", "T-03", "T-04"]
