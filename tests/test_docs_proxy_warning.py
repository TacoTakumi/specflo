"""The docs must warn that a trusted proxy address is trusted for any user.

With reverse-proxy sign-in on, any process that can connect from a trusted
address can set the user header and sign in as anyone. With the loopback
default that is every process on the daemon host, the daemon's own project
agents included. README.md and DAEMON.md must say so, and must not claim a
proxy-only user can never sign in without the proxy.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _flat(text):
    """Collapse line wraps so a phrase matches however the doc is wrapped."""
    return re.sub(r"\s+", " ", text)


def _readme_proxy_section():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("### Reverse-proxy sign-in")
    end = text.index("\n## ", start)
    return text[start:end]


def _daemon_signin_paragraph():
    text = (REPO_ROOT / "DAEMON.md").read_text(encoding="utf-8")
    start = text.index("A browser signs in at `/signin`")
    end = text.index("\n\n", start)
    return text[start:end]


def test_readme_proxy_section_warns_trusted_address_signs_in_as_anyone():
    section = _flat(_readme_proxy_section())
    assert "sign in as any user" in section
    assert "agents" in section


def test_readme_proxy_section_drops_never_can_claim():
    section = _readme_proxy_section()
    para_start = section.index("keep the daemon bound to the loopback address")
    paragraph = section[para_start:].split("\n\n", 1)[0]
    assert "never can" not in _flat(paragraph)


def test_daemon_signin_paragraph_warns_trusted_address_signs_in_as_anyone():
    paragraph = _flat(_daemon_signin_paragraph())
    assert "sign in as any user" in paragraph
    assert "agents" in paragraph


def test_readme_proxy_section_advises_another_host_only():
    # A second address on the same host does not keep local processes out:
    # any local process can connect from any local address.
    section = _flat(_readme_proxy_section())
    assert "another host or address" not in section
    assert "any local address" in section


def test_readme_proxy_section_says_a_remote_proxy_leaves_the_port_reachable():
    section = _flat(_readme_proxy_section())
    assert "proxy on another host the daemon must listen on an address" in section
    assert "firewall" in section


def test_daemon_signin_paragraph_advises_another_host_only():
    paragraph = _flat(_daemon_signin_paragraph())
    assert "another host or address" not in paragraph
    assert "on another host" in paragraph
