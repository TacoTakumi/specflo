"""A request that has to wait says so at once, and its asker may call it off.

The request verb waits for a full pool unless it is told not to, for ten
minutes when no time is named. Whoever runs it is most often an orchestrator
that reads its error stream, so the verb tells it at once that the request
waits, on what, in which place and for how long; the orchestrator then waits
on, or interrupts the verb and turns to other work. The final result is still
all that the output stream carries.

The notice comes down the connection the request went up, before the result:
a client that says it reads notices is answered a request that waits with a
JSON object to a line, the last of them the result or the refusal. A client
that does not say so is answered as it always was, and so is every request
that fits or is refused at once. However a wait ends - a grant, the time, the
client going away, an interrupt - the record of it goes.

The verb runs here as a real process against a real daemon on a loopback
port, for the error stream must be read while the verb still waits and a
signal must reach it.
"""

from __future__ import annotations

import json
import queue
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest

from specflo import cli
from specflo.cli import app
from specflo.daemon import pool_routes
from specflo.errors import SpecfloError
from specflo.pool import waiting
from specflo.pool.runner import RunnerError
from specflo.service.pool_remote import LEASES_PATH, WAITING_MEDIA_TYPE, RemotePool

from .test_lease_request import checkout, pool_daemon, runner  # noqa: F401  (fixtures)
from .test_runner import wait_until

VERB = "from specflo.cli import main; main()"


@pytest.fixture(autouse=True)
def short_interval(monkeypatch):
    """A waiting request is looked at again every few hundredths of a second."""
    monkeypatch.setattr(waiting, "POLL_INTERVAL", 0.05)


@pytest.fixture
def holder(pool_daemon):
    """A client of the daemon, for the one that holds the pool's only member."""
    client = RemotePool(pool_daemon["url"], pool_daemon["token"], timeout=60)
    yield client
    client.client.close()


def waiting_rows(pool_rig):
    with pool_rig.store() as store:
        return store.list_waiting()


def ask_in_background(pool_daemon, pool_rig, *, wait: int, timeout: float = 60.0):
    """A request by a client that reads notices, in a thread; the notices it
    was given and what came of it are in the box."""
    box: dict = {"notices": []}

    def ask() -> None:
        client = RemotePool(pool_daemon["url"], pool_daemon["token"], timeout=timeout)
        try:
            box["grant"] = client.request(
                "rebasers", cwd=str(pool_rig.work), wait=wait,
                on_waiting=box["notices"].append,
            )
        except SpecfloError as exc:
            box["error"] = exc
        finally:
            client.client.close()

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    return thread, box


# -- the connection: a notice, then the result --------------------------------


def test_a_request_that_waits_is_told_the_pool_what_is_full_its_place_and_the_limit(
    pool_daemon, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))

    asked = time.monotonic()
    thread, box = ask_in_background(pool_daemon, pool_rig, wait=60)

    assert wait_until(lambda: box["notices"], timeout=2)
    assert time.monotonic() - asked < 2
    assert thread.is_alive() and "grant" not in box
    (notice,) = box["notices"]
    assert notice.pool == "rebasers"
    assert "pool 'rebasers' is full" in notice.full
    assert (notice.place, notice.wait) == (1, 60)

    # the one that comes next is told it is second
    later, later_box = ask_in_background(pool_daemon, pool_rig, wait=60)
    assert wait_until(lambda: later_box["notices"], timeout=2)
    assert later_box["notices"][0].place == 2

    holder.release(first.lease_id, token=first.token)
    thread.join(timeout=5)
    assert box["grant"].agent == "local-1"
    # one notice, however long the wait
    assert len(box["notices"]) == 1
    assert [row.pool for row in waiting_rows(pool_rig)] == ["rebasers"]
    holder.release(box["grant"].lease_id, token=box["grant"].token)
    later.join(timeout=5)
    assert later_box["grant"].agent == "local-1"
    assert waiting_rows(pool_rig) == []


def test_the_answer_to_a_request_that_waits_is_a_json_object_to_a_line(
    pool_daemon, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))
    headers = {
        "Authorization": f"Bearer {pool_daemon['token']}", "Accept": WAITING_MEDIA_TYPE,
    }
    body = {"pool": "rebasers", "cwd": str(pool_rig.work), "wait": 60}

    with httpx.Client(base_url=pool_daemon["url"], timeout=30) as client:
        with client.stream("POST", LEASES_PATH, json=body, headers=headers) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(WAITING_MEDIA_TYPE)
            lines = response.iter_lines()
            told = json.loads(next(lines))
            assert told == {"waiting": {
                "pool": "rebasers", "full": told["waiting"]["full"], "place": 1, "wait": 60,
            }}
            holder.release(first.lease_id, token=first.token)
            rest = [json.loads(line) for line in lines]

    (last,) = rest
    assert set(last["result"]) == {"lease_id", "agent", "token"}
    assert last["result"]["agent"] == "local-1"
    assert waiting_rows(pool_rig) == []


def test_a_request_that_fits_at_once_is_answered_as_ever_and_told_nothing(
    pool_daemon, pool_rig
):
    headers = {
        "Authorization": f"Bearer {pool_daemon['token']}", "Accept": WAITING_MEDIA_TYPE,
    }
    body = {"pool": "rebasers", "cwd": str(pool_rig.work), "wait": 60}

    response = httpx.post(pool_daemon["url"] + LEASES_PATH, json=body, headers=headers, timeout=30)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["result"]["agent"] == "local-1"


def test_a_request_refused_at_once_keeps_its_status_for_a_client_that_reads_notices(
    pool_daemon, pool_rig, holder
):
    holder.request("rebasers", cwd=str(pool_rig.work))
    headers = {
        "Authorization": f"Bearer {pool_daemon['token']}", "Accept": WAITING_MEDIA_TYPE,
    }
    good = {"pool": "rebasers", "cwd": str(pool_rig.work)}

    for body, named in (
        (good, "rebasers"),
        ({**good, "pool": "reviewers", "wait": 60}, "reviewers"),
        ({**good, "idle_limit": 5 * 3600, "wait": 60}, "4h"),
    ):
        response = httpx.post(
            pool_daemon["url"] + LEASES_PATH, json=body, headers=headers, timeout=30
        )
        assert response.status_code == 400, (body, response.text)
        detail = response.json()["detail"]
        assert isinstance(detail, str) and named in detail
    assert waiting_rows(pool_rig) == []


def test_a_client_that_reads_no_notices_is_answered_a_wait_with_one_object(
    pool_daemon, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))
    headers = {"Authorization": f"Bearer {pool_daemon['token']}"}
    body = {"pool": "rebasers", "cwd": str(pool_rig.work), "wait": 60}
    box: dict = {}

    def ask() -> None:
        box["response"] = httpx.post(
            pool_daemon["url"] + LEASES_PATH, json=body, headers=headers, timeout=30
        )

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    assert wait_until(lambda: len(waiting_rows(pool_rig)) == 1)
    holder.release(first.lease_id, token=first.token)
    thread.join(timeout=5)

    assert box["response"].headers["content-type"].startswith("application/json")
    assert box["response"].json()["result"]["agent"] == "local-1"


def test_a_wait_that_runs_out_is_refused_in_the_words_a_client_that_reads_none_gets(
    pool_daemon, pool_rig, holder
):
    holder.request("rebasers", cwd=str(pool_rig.work))
    with pytest.raises(SpecfloError) as plain:
        holder.request("rebasers", cwd=str(pool_rig.work), wait=1)

    thread, box = ask_in_background(pool_daemon, pool_rig, wait=1)
    thread.join(timeout=10)

    assert len(box["notices"]) == 1
    assert str(box["error"]) == str(plain.value)
    assert "Waited 1 s" in str(box["error"])
    assert "pool 'rebasers' is full" in str(box["error"])
    assert waiting_rows(pool_rig) == []


def test_a_member_that_fails_after_the_notice_is_told_as_it_is_with_no_notice(
    pool_daemon, pool_rig, holder, monkeypatch
):
    holder.request("rebasers", cwd=str(pool_rig.work))
    real_attempt = waiting.Waiting.attempt
    looks: dict = {}

    def failing_after_the_first_look(self):
        looks[id(self)] = looks.get(id(self), 0) + 1
        if looks[id(self)] == 1:
            return real_attempt(self)
        raise RunnerError("the member in /home/someone/secret did not start")

    monkeypatch.setattr(pool_routes.waiting.Waiting, "attempt", failing_after_the_first_look)
    with pytest.raises(SpecfloError) as plain:
        holder.request("rebasers", cwd=str(pool_rig.work), wait=60)

    thread, box = ask_in_background(pool_daemon, pool_rig, wait=60)
    thread.join(timeout=10)

    assert len(box["notices"]) == 1
    assert str(box["error"]) == str(plain.value)
    assert "502" in str(box["error"])
    assert "secret" not in str(box["error"])
    assert waiting_rows(pool_rig) == []


def test_a_client_that_goes_away_after_the_notice_leaves_no_waiting_record(
    pool_daemon, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))

    # the client gives up after a second and closes its connection
    thread, box = ask_in_background(pool_daemon, pool_rig, wait=600, timeout=1.0)
    assert wait_until(lambda: box["notices"], timeout=2)
    assert len(waiting_rows(pool_rig)) == 1
    thread.join(timeout=10)
    assert "error" in box

    assert wait_until(lambda: waiting_rows(pool_rig) == [], timeout=5)
    # and what frees later is not granted to it
    holder.release(first.lease_id, token=first.token)
    time.sleep(0.3)
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []


# -- the verb: how long it waits ----------------------------------------------


def lease_bodies(monkeypatch) -> list[dict]:
    """The body of every lease request the verb sends, as it goes out."""
    bodies = []
    real_send = httpx.Client.send

    def recording_send(self, request, **kwargs):
        if request.url.path == LEASES_PATH:
            bodies.append(json.loads(request.content))
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", recording_send)
    return bodies


def test_with_no_time_named_the_verb_asks_to_wait_ten_minutes(checkout, pool_rig, monkeypatch):
    bodies = lease_bodies(monkeypatch)

    result = runner.invoke(app, ["lease", "request", "rebasers", "--json"])

    assert result.exit_code == 0, result.output
    assert cli.LEASE_WAIT_DEFAULT == 600
    assert [body["wait"] for body in bodies] == [600]
    # a request that fits at once is told nothing
    assert result.stderr == ""
    assert set(json.loads(result.stdout)) == {"lease", "agent", "pool", "remote"}


def test_wait_0_refuses_a_full_pool_at_once_and_says_nothing_of_waiting(
    checkout, pool_rig, monkeypatch
):
    held = runner.invoke(app, ["lease", "request", "rebasers"])
    assert held.exit_code == 0, held.output
    bodies = lease_bodies(monkeypatch)

    asked = time.monotonic()
    result = runner.invoke(app, ["lease", "request", "rebasers", "--wait", "0"])

    assert result.exit_code != 0
    assert time.monotonic() - asked < 2
    assert "pool 'rebasers' is full" in " ".join(result.output.split())
    assert "Waiting" not in result.output
    assert ["wait" in body for body in bodies] == [False]
    assert waiting_rows(pool_rig) == []


# -- the verb: a real process, read while it waits -----------------------------


class Verb:
    """``specflo lease request`` as a process of its own, in the checkout; its
    error stream is read line by line while it runs."""

    def __init__(self, checkout_dir, *args: str) -> None:
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            [sys.executable, "-c", VERB, "lease", "request", *args],
            cwd=checkout_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.errors: list[str] = []
        self._lines: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for line in self.process.stderr:
            self.errors.append(line)
            self._lines.put(line)

    def error_line(self, holding: str, timeout: float) -> str | None:
        """The next line of the error stream that holds *holding*, if one comes in time."""
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                return None
            if holding in line:
                return line
        return None

    def finish(self, timeout: float = 15) -> tuple[int, str, str]:
        """The exit code, the whole output stream and the whole error stream."""
        try:
            code = self.process.wait(timeout=timeout)
        finally:
            if self.process.poll() is None:
                self.process.kill()
        self._reader.join(timeout=5)
        return code, self.process.stdout.read(), "".join(self.errors)


def test_the_verb_says_at_once_on_its_error_stream_that_it_waits_and_prints_the_result_alone(
    checkout, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))

    verb = Verb(checkout, "rebasers", "--wait", "30")
    notice = verb.error_line("rebasers", timeout=2)

    assert notice is not None, "no notice within 2 s"
    assert verb.process.poll() is None
    assert "Waiting" in notice
    assert "pool 'rebasers' is full" in notice
    assert "number 1 " in notice
    assert "30 s" in notice
    assert len(waiting_rows(pool_rig)) == 1

    holder.release(first.lease_id, token=first.token)
    code, output, errors = verb.finish()

    assert code == 0, errors
    assert "local-1" in output and "Waiting" not in output
    assert errors.count("Waiting") == 1
    assert waiting_rows(pool_rig) == []


def test_with_json_the_notice_is_one_object_on_the_error_stream_and_the_result_one_on_the_output(
    checkout, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))

    verb = Verb(checkout, "rebasers", "--wait", "30", "--json")
    notice = verb.error_line("{", timeout=2)

    assert notice is not None, "no notice within 2 s"
    told = json.loads(notice)
    assert told["event"] == "waiting"
    assert (told["pool"], told["remote"], told["place"], told["wait"]) == (
        "rebasers", "home", 1, 30,
    )
    assert "pool 'rebasers' is full" in told["full"]

    holder.release(first.lease_id, token=first.token)
    code, output, errors = verb.finish()

    assert code == 0, errors
    assert json.loads(output)["agent"] == "local-1"
    assert [line for line in errors.splitlines() if line.startswith("{")] == [notice.rstrip("\n")]


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_an_interrupted_verb_exits_non_zero_says_cancelled_and_waits_no_longer(
    checkout, pool_rig, holder, interrupt
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))
    # no time is named: the verb would wait ten minutes
    verb = Verb(checkout, "rebasers")
    assert verb.error_line("600 s", timeout=2) is not None
    assert len(waiting_rows(pool_rig)) == 1

    verb.process.send_signal(interrupt)
    sent = time.monotonic()
    code, output, errors = verb.finish()

    assert code not in (0, None)
    assert "cancelled" in errors
    assert "Traceback" not in errors
    assert output == ""
    assert wait_until(lambda: waiting_rows(pool_rig) == [], timeout=5)
    assert time.monotonic() - sent < 5
    assert not (checkout / ".specflo" / "leases" / "local-1.token").exists()
    # and what frees later is not granted to it
    holder.release(first.lease_id, token=first.token)
    time.sleep(0.3)
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
