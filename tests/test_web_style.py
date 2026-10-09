"""The web UI's look: the frame and markdown stylesheets the pages share.

The daemon serves two stylesheets from its assets directory with no session:
specflo.css, the page frame, whose every colour is a light-dark() token on
:root with a teal accent, and a vendored github-markdown.css for rendered
markdown, whose MIT notice sits in LICENSES.md beside it.
"""

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import web
from specflo.daemon.app import create_app

STYLESHEETS = ("specflo.css", "github-markdown.css")
TEAL_ACCENT = "--accent:       light-dark(#0e7c7b, #4cc3bd);"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(daemon.prepare_root(tmp_path / "daemon")), follow_redirects=False)


@pytest.mark.parametrize("name", STYLESHEETS)
def test_each_stylesheet_is_served_without_a_session(client, name):
    response = client.get(f"{web.ASSETS_PATH}/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_the_frame_accent_is_teal_in_both_themes():
    assert TEAL_ACCENT in (web.ASSETS_DIR / "specflo.css").read_text()


def test_the_markdown_stylesheet_keeps_its_mit_notice():
    first_line = (web.ASSETS_DIR / "github-markdown.css").read_text().splitlines()[0]
    notice = (web.ASSETS_DIR / "LICENSES.md").read_text()

    assert "github-markdown-css v5.9.0 | MIT License" in first_line
    assert "| github-markdown.css | github-markdown-css | 5.9.0 |" in notice
    assert "Permission is hereby granted, free of charge" in notice
