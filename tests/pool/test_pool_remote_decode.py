"""The pool client builds its records from the keys it knows.

A daemon of a later release may answer a lease, a team member, a waiting
notice, a grant or a lease end with a field this client has never heard of.
The client lets such a field go, so each verb prints what it prints today. A
record that lacks a key the client needs, or is no object at all, and the same
in the reload and console answers, is the verb's own error line and never a
traceback. The daemon here is a loopback server with canned answers.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from specflo import config
from specflo.cli import app

runner = CliRunner()

EXTRA = {"added_later": "a field of a later release"}

NOTICE = {"full": "pool 'rebasers' is full", "place": 1, "wait": 5, "pool": "rebasers"}
GRANT = {"lease_id": "lease-1", "agent": "rebaser-1", "token": "tok-1"}
TEAM = {
    "team_lease_id": "team-1",
    "members": [
        {"role": "coder", "pool": "coders", "lease_id": "lease-2", "agent": "coder-1",
         "token": "tok-2"},
        {"role": "reviewer", "pool": "reviewers", "lease_id": "lease-3",
         "agent": "reviewer-1", "token": "tok-3"},
    ],
}
HELD = {
    "lease_id": "lease-1", "agent": "rebaser-1", "pool": "rebasers", "state": "active",
    "acquired": "2026-09-24T10:00:00Z", "last_activity": "2026-09-24T10:05:00Z",
    "idle_limit": 600,
}
END = {"lease_id": "lease-1", "state": "released", "held": True}
RELOAD = {
    "pid": 4242, "directory": "/srv/daemon/pool", "definitions": 5, "accounts": 1,
    "members": 2, "pools": 1, "teams": 0,
}
ATTACHED = {"slot": "desk-1", "agent": "my-pi", "state": "attached"}
DETACHED = {"slot": "desk-1", "state": "draining"}


def with_extra(record):
    return {**record, **EXTRA}


def answers(extra: bool) -> dict:
    """What the canned daemon answers on each path, with or without a field
    of a later release in every record."""
    add = with_extra if extra else (lambda record: record)
    team = {**TEAM, "members": [add(member) for member in TEAM["members"]]}
    return {
        "waiting": add(NOTICE),
        "grant": add(GRANT),
        "team": add(team),
        "held": [add(HELD)],
        "release": add(END),
        "reload": add(RELOAD),
        "attach": add(ATTACHED),
        "detach": add(DETACHED),
    }


@pytest.fixture
def daemon():
    """A loopback server whose answers are the dict it yields, by kind."""
    canned: dict = answers(extra=False)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            path = self.path
            if path.endswith("/leases"):
                result = canned["team"] if "team" in body else canned["grant"]
                if "waiting" in canned and "x-ndjson" in (self.headers.get("Accept") or ""):
                    lines = [{"waiting": canned["waiting"]}, {"result": result}]
                    return self._send("application/x-ndjson", "".join(
                        json.dumps(line) + "\n" for line in lines
                    ))
                return self._send("application/json", json.dumps({"result": result}))
            kind = next(
                name for suffix, name in (
                    ("/held", "held"), ("/release", "release"), ("/reload", "reload"),
                    ("/attach", "attach"), ("/detach", "detach"),
                ) if path.endswith(suffix)
            )
            self._send("application/json", json.dumps({"result": canned[kind]}))

        def _send(self, media: str, text: str) -> None:
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield canned, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def checkout(path, url, monkeypatch):
    path.mkdir()
    config.init_config(path)
    config.add_remote(path, "home", url, "s3cret")
    monkeypatch.chdir(path)
    return path


LEASE_VERBS = [
    ["lease", "request", "rebasers", "--wait", "5"],
    ["lease", "list"],
    ["lease", "list", "--json"],
    ["lease", "release", "lease-1"],
    ["lease", "request", "--team", "pair", "--wait", "5"],
    ["lease", "request", "rebasers", "--wait", "5", "--json"],
]


def run_all(verbs) -> list[tuple[int, str]]:
    return [
        (result.exit_code, result.output)
        for result in (runner.invoke(app, verb) for verb in verbs)
    ]


def test_a_field_of_a_later_release_changes_no_lease_verb(tmp_path, monkeypatch, daemon):
    canned, url = daemon

    checkout(tmp_path / "today", url, monkeypatch)
    today = run_all(LEASE_VERBS)
    canned.update(answers(extra=True))
    checkout(tmp_path / "later", url, monkeypatch)
    later = run_all(LEASE_VERBS)

    assert [code for code, _ in today] == [0] * len(LEASE_VERBS), today
    assert later == today
    assert "added_later" not in "".join(output for _, output in later)


def test_a_field_of_a_later_release_changes_no_reload_or_console_verb(
    tmp_path, monkeypatch, daemon
):
    canned, url = daemon
    verbs = [
        ["serve", "--root", str(tmp_path / "root"), "pool", "reload"],
        ["console", "attach", "desk-1", "my-pi"],
        ["console", "detach", "desk-1"],
    ]

    checkout(tmp_path / "today", url, monkeypatch)
    today = run_all(verbs)
    canned.update(answers(extra=True))
    later = run_all(verbs)

    assert [code for code, _ in today] == [0, 0, 0], today
    assert later == today


def without(record: dict, key: str) -> dict:
    return {k: v for k, v in record.items() if k != key}


# Each case: what the daemon answers wrongly, and the verb that reads it.
MALFORMED = {
    "notice-missing-key": ({"waiting": without(NOTICE, "place")}, ["lease", "request", "rebasers"]),
    "notice-not-an-object": ({"waiting": 7}, ["lease", "request", "rebasers"]),
    "grant-missing-key": ({"grant": without(GRANT, "token")}, ["lease", "request", "rebasers"]),
    "grant-not-an-object": ({"grant": 7}, ["lease", "request", "rebasers"]),
    "team-missing-key": (
        {"team": without(TEAM, "members")}, ["lease", "request", "--team", "pair"],
    ),
    "team-member-missing-key": (
        {"team": {**TEAM, "members": [without(TEAM["members"][0], "role")]}},
        ["lease", "request", "--team", "pair"],
    ),
    "team-member-not-an-object": (
        {"team": {**TEAM, "members": [7]}}, ["lease", "request", "--team", "pair"],
    ),
    "held-missing-key": ({"held": [without(HELD, "agent")]}, ["lease", "list"]),
    "held-not-an-object": ({"held": [7]}, ["lease", "list"]),
    "held-not-a-list": ({"held": 7}, ["lease", "list"]),
    "end-missing-key": ({"release": without(END, "held")}, ["lease", "release", "lease-1"]),
    "end-not-an-object": ({"release": 7}, ["lease", "release", "lease-1"]),
    "reload-missing-key": ({"reload": without(RELOAD, "pid")}, None),
    "reload-not-an-object": ({"reload": 7}, None),
    "attach-missing-key": (
        {"attach": without(ATTACHED, "agent")}, ["console", "attach", "desk-1", "my-pi"],
    ),
    "attach-not-an-object": ({"attach": 7}, ["console", "attach", "desk-1", "my-pi"]),
    "detach-missing-key": ({"detach": without(DETACHED, "state")}, ["console", "detach", "desk-1"]),
    "detach-not-an-object": ({"detach": 7}, ["console", "detach", "desk-1"]),
}


@pytest.mark.parametrize("case", MALFORMED)
def test_a_record_the_client_cannot_build_is_the_verbs_error_line(
    tmp_path, monkeypatch, daemon, case
):
    canned, url = daemon
    checkout(tmp_path / "checkout", url, monkeypatch)
    # A lease is kept first, so that list and release have a token to show.
    assert runner.invoke(app, ["lease", "request", "rebasers", "--wait", "0"]).exit_code == 0
    if "waiting" not in MALFORMED[case][0]:
        del canned["waiting"]
    changed, verb = MALFORMED[case]
    canned.update(changed)
    verb = verb or ["serve", "--root", str(tmp_path / "root"), "pool", "reload"]

    result = runner.invoke(app, verb)

    assert result.exit_code not in (0, None)
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1, result.output
    assert url in lines[0] and "malformed" in lines[0]
