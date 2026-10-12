"""The gateway configuration: the forges it watches and the files of their credentials.

``gateway.yaml`` in the daemon root lists forges. Each has a kind (gitea or
github), a base URL, the repos to watch, the operator's user id on that forge,
the trigger label (``rfc`` unless it says otherwise), the pool to lease from,
and the names of its credential files. A credential file lies in the daemon
root with mode 0600. Every fault names the field at fault, and any fault
leaves the gateway without a configuration.
"""

import os

import pytest
import yaml

from specflo import daemon
from specflo.errors import SpecfloError
from specflo.gateway import config

TOKEN = "fake-gitea-token-3f9a"
KEY = "-----BEGIN FAKE KEY-----\nfake-github-key-77c1\n-----END FAKE KEY-----\n"


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def _secret(root, name, text, mode=0o600):
    path = root / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _gitea(**fields):
    forge = {
        "kind": "gitea",
        "url": "https://gitea.example",
        "repos": ["me/thing"],
        "operator_id": 7,
        "pool": "rfc-research",
        "token_file": "gitea.token",
    }
    forge.update(fields)
    return {key: value for key, value in forge.items() if value is not None}


def _github(**fields):
    forge = {
        "kind": "github",
        "repos": ["me/other"],
        "operator_id": 4242,
        "pool": "rfc-research",
        "app_id": 99,
        "key_file": "github-app.pem",
    }
    forge.update(fields)
    return {key: value for key, value in forge.items() if value is not None}


def _write(root, data):
    (root / config.CONFIG_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")


def _load(root):
    return config.load_gateway_config(root)


def _fields(errors):
    return [error.field for error in errors]


def test_a_root_without_the_file_has_no_gateway_and_no_fault(root):
    assert _load(root) == (None, [])


def test_a_valid_configuration_reads_every_field(root):
    token = _secret(root, "gitea.token", TOKEN)
    key = _secret(root, "github-app.pem", KEY)
    _write(root, {"poll_every": "30s", "forges": [_gitea(label="research"), _github()]})

    found, errors = _load(root)

    assert errors == []
    assert found.poll_every == 30
    gitea, github = found.forges
    assert gitea == config.Forge(
        name="gitea", kind="gitea", url="https://gitea.example", repos=("me/thing",),
        operator_id=7, pool="rfc-research", label="research", token_file=token,
    )
    assert github.name == "github"
    assert github.url == config.GITHUB_API
    assert github.label == config.DEFAULT_LABEL == "rfc"
    assert github.app_id == 99
    assert github.key_file == key
    assert github.token_file is None


def test_the_poll_interval_is_a_minute_unless_written(root):
    _secret(root, "gitea.token", TOKEN)
    _write(root, {"forges": [_gitea()]})

    found, errors = _load(root)

    assert errors == []
    assert found.poll_every == config.DEFAULT_POLL_EVERY == 60


def test_the_configuration_holds_no_secret(root):
    _secret(root, "gitea.token", TOKEN)
    _secret(root, "github-app.pem", KEY)
    _write(root, {"forges": [_gitea(), _github()]})

    found, errors = _load(root)

    assert errors == []
    assert TOKEN not in repr(found)
    assert "fake-github-key" not in repr(found)


@pytest.mark.parametrize(
    "forge, field",
    [
        (_gitea(kind="gitlab"), "kind"),
        (_gitea(operator_id=None), "operator_id"),
        (_gitea(operator_id="me"), "operator_id"),
        (_gitea(operator_id=True), "operator_id"),
        (_gitea(repos=[]), "repos"),
        (_gitea(repos=None), "repos"),
        (_gitea(repos=["no-owner"]), "repos"),
        (_gitea(url=None), "url"),
        (_gitea(url="ftp://gitea.example"), "url"),
        (_gitea(pool=None), "pool"),
        (_gitea(label=""), "label"),
        (_gitea(token_file=None), "token_file"),
        (_gitea(key_file="github-app.pem"), "key_file"),
        (_gitea(colour="blue"), "colour"),
        (_github(app_id=None), "app_id"),
        (_github(key_file=None), "key_file"),
    ],
)
def test_each_fault_names_its_field_and_leaves_no_configuration(root, forge, field):
    _secret(root, "gitea.token", TOKEN)
    _secret(root, "github-app.pem", KEY)
    _write(root, {"forges": [forge]})

    found, errors = _load(root)

    assert found is None
    assert field in _fields(errors)
    assert all(isinstance(error, SpecfloError) for error in errors)
    assert f": {field}: " in str(errors[_fields(errors).index(field)])


def test_a_missing_credential_file_is_a_fault_on_its_field(root):
    _write(root, {"forges": [_gitea()]})

    found, errors = _load(root)

    assert found is None
    assert _fields(errors) == ["token_file"]
    assert "gitea.token" in str(errors[0])


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o700])
def test_a_credential_file_with_another_mode_than_0600_is_refused(root, mode):
    _secret(root, "gitea.token", TOKEN, mode=mode)
    _write(root, {"forges": [_gitea()]})

    found, errors = _load(root)

    assert found is None
    assert _fields(errors) == ["token_file"]
    assert "0600" in str(errors[0])
    assert TOKEN not in str(errors[0])


@pytest.mark.parametrize("name", ["/etc/passwd", "../outside.token", "sub/../../x.token"])
def test_a_credential_file_outside_the_daemon_root_is_refused(root, name):
    _write(root, {"forges": [_gitea(token_file=name)]})

    found, errors = _load(root)

    assert found is None
    assert _fields(errors) == ["token_file"]


def test_a_credential_that_is_not_a_regular_file_is_refused(root):
    (root / "gitea.token").mkdir()
    _write(root, {"forges": [_gitea()]})

    found, errors = _load(root)

    assert found is None
    assert _fields(errors) == ["token_file"]


@pytest.mark.parametrize(
    "data, field",
    [
        ({}, "forges"),
        ({"forges": []}, "forges"),
        ({"forges": "gitea"}, "forges"),
        ({"forges": [_gitea()], "poll_every": 60}, "poll_every"),
        ({"forges": [_gitea()], "webhooks": True}, "webhooks"),
        ({"forges": ["gitea"]}, "forges"),
    ],
)
def test_faults_of_the_whole_file_name_their_key(root, data, field):
    _secret(root, "gitea.token", TOKEN)
    _write(root, data)

    found, errors = _load(root)

    assert found is None
    assert field in _fields(errors)


def test_a_file_that_is_not_yaml_is_a_fault(root):
    (root / config.CONFIG_FILE).write_text("forges: [unclosed\n", encoding="utf-8")

    found, errors = _load(root)

    assert found is None
    assert _fields(errors) == ["file"]


def test_two_forges_under_one_name_are_refused(root):
    _secret(root, "gitea.token", TOKEN)
    _write(root, {"forges": [_gitea(), _gitea(url="https://other.example")]})

    found, errors = _load(root)

    assert found is None
    assert _fields(errors) == ["name"]


def test_named_forges_of_one_kind_stand_together(root):
    _secret(root, "gitea.token", TOKEN)
    _write(root, {"forges": [_gitea(name="home"), _gitea(name="work", url="https://w.example")]})

    found, errors = _load(root)

    assert errors == []
    assert [forge.name for forge in found.forges] == ["home", "work"]


def test_every_fault_in_the_file_is_reported(root):
    _write(root, {"forges": [_gitea(kind="gitlab"), _gitea(name="two", operator_id=None)]})

    found, errors = _load(root)

    assert found is None
    assert {"kind", "operator_id"} <= set(_fields(errors))


def test_read_credential_gives_the_secret_and_checks_the_mode_again(root):
    path = _secret(root, "gitea.token", TOKEN + "\n")

    assert config.read_credential(path) == TOKEN

    os.chmod(path, 0o644)
    with pytest.raises(SpecfloError) as raised:
        config.read_credential(path)
    assert "0600" in str(raised.value)
    assert TOKEN not in str(raised.value)
