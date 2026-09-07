"""Structural facts about the web UI: what can mutate, how the chat routes run, and how the transcript renders.

The templates are scanned as text, so a control added later shows up here
before it ships: the mutating controls are exactly start project, send
message, take gate and start agent, each behind the session secret; the
sign-in form is the front door, not a control on the workflow. The chat
routes are async handlers and the stream's request timeout is lifted past
htmx 4's default, since a turn can outlast it. The transcript template
renders author, time and text per entry inside the monospace region.
"""

import inspect
import re

import pytest

from specflo.daemon import web

TEMPLATES = sorted(web.TEMPLATES_DIR.glob("*.html"))

# The four controls, by the URL helper each form posts to.
CONTROLS = {
    "start project": "start_project_url(",
    "send message": "chat_url(",
    "take gate": "take_url(",
    "start agent": "start_agent_url(",
}
FRONT_DOOR = "signin_path"
# htmx 4 aborts a request after this many milliseconds unless told otherwise.
HTMX_DEFAULT_TIMEOUT_MS = 60_000
# Verbs that make an element post on its own, without a form.
HX_VERBS = ("hx-post", "hx-put", "hx-patch", "hx-delete")


def forms(text):
    """Every form in a template's text: ``(opening tag, body)``."""
    return re.findall(r"(<form[^>]*>)(.*?)</form>", text, re.S)


def test_the_mutating_controls_are_exactly_the_four_and_the_front_door():
    seen = {}
    for path in TEMPLATES:
        for tag, body in forms(path.read_text()):
            action = re.search(r'action="([^"]*)"', tag).group(1)
            if FRONT_DOOR in action:
                seen.setdefault("sign in", []).append(path.name)
                continue
            names = [name for name, helper in CONTROLS.items() if helper in action]
            assert len(names) == 1, (path.name, action)
            seen.setdefault(names[0], []).append(path.name)
    assert set(seen) == set(CONTROLS) | {"sign in"}, seen
    assert seen["sign in"] == ["signin.html"]


def test_every_form_but_the_front_door_posts_and_carries_the_session_secret():
    for path in TEMPLATES:
        for tag, body in forms(path.read_text()):
            assert 'method="post"' in tag, (path.name, tag)
            if FRONT_DOOR in tag:
                continue
            assert '<input type="hidden" name="session" value="{{ session }}">' in body, (path.name, tag)


def test_no_element_posts_outside_a_form():
    for path in TEMPLATES:
        text = path.read_text()
        for verb in HX_VERBS:
            assert verb not in text, (path.name, verb)
        # Every button submits one of the forms: none stands alone.
        stripped = re.sub(r"<form[^>]*>.*?</form>", "", text, flags=re.S)
        assert "<button" not in stripped, path.name


def test_the_only_connection_the_pages_hold_is_the_transcript_stream():
    connections = [
        (path.name, match)
        for path in TEMPLATES
        for match in re.findall(r'hx-sse:connect="([^"]*)"', path.read_text())
    ]
    assert connections == [("project.html", "{{ chat_stream_url(view.project.slug, view.last_entry_id) }}")]


@pytest.mark.parametrize("handler", [web.chat_stream, web.post_message])
def test_the_chat_routes_are_async_handlers(handler):
    assert inspect.iscoroutinefunction(handler), handler.__name__


def test_the_stream_request_outlives_the_default_timeout():
    project = (web.TEMPLATES_DIR / "project.html").read_text()
    tag = re.search(r"<div id=\"transcript\"[^>]*>", project).group(0)
    config = re.search(r'hx-config="([^"]*)"', tag)
    assert config, "the stream element sets no request config"
    timeout = re.search(r"timeout:\s*([0-9]+)(ms|s|m)?", config.group(1))
    assert timeout, config.group(1)
    value, unit = int(timeout.group(1)), timeout.group(2) or "ms"
    ms = value * {"ms": 1, "s": 1000, "m": 60_000}[unit]
    # Zero turns the timeout off, which a stream held open all visit needs;
    # anything else must at least outlast a long turn.
    assert ms == 0 or ms > HTMX_DEFAULT_TIMEOUT_MS, config.group(1)


def test_the_transcript_line_renders_author_time_and_text_inside_the_monospace_region():
    line = (web.TEMPLATES_DIR / "chat_line.html").read_text()
    assert '<span class="author">{{ line_label(entry) }}</span>' in line
    assert '<time datetime="{{ entry.time }}">' in line
    assert '<span class="text">{{ entry.text }}</span>' in line
    assert line.count("<div") == 1, "one line per entry"

    project = (web.TEMPLATES_DIR / "project.html").read_text()
    region = re.search(r'<div id="transcript" class="transcript".*?</div>', project, re.S).group(0)
    assert '{% include "chat_line.html" %}' in region

    base = (web.TEMPLATES_DIR / "base.html").read_text()
    rule = re.search(r"\.transcript\s*\{([^}]*)\}", base)
    assert rule and "monospace" in rule.group(1)
