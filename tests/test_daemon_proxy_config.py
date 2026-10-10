"""The daemon root's sign-in proxy keys, and the refusal of an unsafe trusted list.

A daemon behind a reverse proxy can take the signed-in user's name from a
header the proxy sets. The root's config says whether it does, which header
it reads, and which peers it trusts to set it. A trusted list that would let
any peer name any user stops ``specflo serve`` before it binds a socket.
"""

import ipaddress

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.errors import SpecfloError

runner = CliRunner()


def _root_with(tmp_path, text: str):
    """A prepared daemon root whose config also holds ``text``."""
    root = daemon.prepare_root(tmp_path / "daemon")
    path = config.config_path(root)
    path.write_text(path.read_text() + text)
    return root


@pytest.fixture
def served(monkeypatch):
    """Capture what ``serve`` hands to uvicorn instead of binding a socket."""
    import uvicorn

    captured = {}

    def run(application, **kwargs):
        captured["app"] = application
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", run)
    return captured


# --- the reader ------------------------------------------------------------


def test_no_keys_reads_the_defaults(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")

    setting = config.proxy_auth(root)

    assert setting.enabled is False
    assert setting.header == "X-WEBAUTH-USER"
    assert setting.trusted == (
        ipaddress.ip_network("127.0.0.1"), ipaddress.ip_network("::1"),
    )


def test_a_directory_with_no_config_reads_the_defaults(tmp_path):
    assert config.proxy_auth(tmp_path / "nowhere") == config.ProxyAuth()


def test_the_keys_read_as_set(tmp_path):
    root = _root_with(
        tmp_path,
        "auth_proxy: true\n"
        "auth_proxy_header: X-Forwarded-User\n"
        "auth_trusted_proxies: ['10.0.0.5', '192.168.1.0/24']\n",
    )

    setting = config.proxy_auth(root)

    assert setting.enabled is True
    assert setting.header == "X-Forwarded-User"
    assert setting.trusted == (
        ipaddress.ip_network("10.0.0.5"), ipaddress.ip_network("192.168.1.0/24"),
    )


def test_the_keys_are_not_unknown_to_config_list(tmp_path):
    root = _root_with(
        tmp_path,
        "auth_proxy: false\nauth_proxy_header: X-User\nauth_trusted_proxies: []\n",
    )

    assert config.report_config(root)["unknown"] == []


@pytest.mark.parametrize(
    "text",
    [
        "auth_proxy: 'yes please'\n",
        "auth_proxy: true\nauth_proxy_header: ''\n",
        "auth_proxy: true\nauth_proxy_header: 7\n",
        "auth_proxy: true\nauth_trusted_proxies: 10.0.0.5\n",
        "auth_proxy: true\nauth_trusted_proxies: [7]\n",
        "auth_proxy: true\nauth_trusted_proxies: ['0.0.0.0/0.0.0.0']\n",
    ],
)
def test_a_bad_setting_with_the_proxy_on_raises(tmp_path, text):
    root = _root_with(tmp_path, text)

    with pytest.raises(SpecfloError):
        config.proxy_auth(root)


# --- the serve command -----------------------------------------------------


@pytest.mark.parametrize(
    "trusted", ["[]", "['*']", "['0.0.0.0/0']", "['::/0']", "['nonsense']"],
)
def test_serve_refuses_an_unsafe_trusted_list(tmp_path, served, trusted):
    root = _root_with(tmp_path, f"auth_proxy: true\nauth_trusted_proxies: {trusted}\n")

    result = runner.invoke(app, ["serve", "--root", str(root)])

    assert result.exit_code != 0
    assert "auth_trusted_proxies" in result.output
    assert "app" not in served


def test_serve_starts_on_a_safe_trusted_list(tmp_path, served):
    root = _root_with(
        tmp_path,
        "auth_proxy: true\nauth_trusted_proxies: ['10.0.0.5', '192.168.1.0/24']\n",
    )

    result = runner.invoke(app, ["serve", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert served["app"].state.proxy_auth.enabled is True
    assert served["app"].state.proxy_auth.trusted == (
        ipaddress.ip_network("10.0.0.5"), ipaddress.ip_network("192.168.1.0/24"),
    )


def test_serve_with_the_proxy_off_ignores_a_bad_trusted_list(tmp_path, served):
    root = _root_with(tmp_path, "auth_proxy: false\nauth_trusted_proxies: ['*']\n")

    result = runner.invoke(app, ["serve", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert served["app"].state.proxy_auth.enabled is False


def test_serve_keeps_the_socket_peer_as_the_client(tmp_path, served):
    root = daemon.prepare_root(tmp_path / "daemon")

    result = runner.invoke(app, ["serve", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert served["proxy_headers"] is False
    assert served["app"].state.proxy_auth == config.ProxyAuth()
