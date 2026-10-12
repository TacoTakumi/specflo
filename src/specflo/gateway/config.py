"""The gateway configuration: the forges the gateway watches and their credentials.

An operator writes ``gateway.yaml`` in the daemon root, and the daemon runs
the gateway only when the file is there and stands as written. The file lists
forges. Each has a kind, ``gitea`` or ``github``; a base URL (GitHub's API
unless written); the repos to watch, as ``owner/name``; the operator's user id
on that forge, the only one whose label starts a run; the trigger label,
``rfc`` unless written; the pool to lease research agents from; and the names
of its credential files. A Gitea forge has the bot's token in ``token_file``;
a GitHub forge has its App's ``app_id`` and the App's private key in
``key_file``. ``poll_every`` beside the forges is how long the gateway waits
between polls, a minute unless written.

A credential file is named by a path relative to the daemon root and lies
under it, a regular file with mode 0600. The configuration holds where each
file is and never what is in it: :func:`read_credential` reads a secret when a
forge client needs it, and checks the mode again. No fault names a secret.

Loading is strict, as the pool file's is: a key this module does not know is
refused rather than ignored, every fault names the entry and the field at
fault, and one fault anywhere leaves the gateway with no configuration at all.
A forge entry is a list item with an optional ``name`` (its kind when none is
written), so two forges of one kind need names of their own.
"""

from __future__ import annotations

import logging
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..errors import SpecfloError
from ..pool.definitions import NOT_UTF8, not_a_file

#: The configuration file's name in the daemon root.
CONFIG_FILE = "gateway.yaml"

GITEA = "gitea"
GITHUB = "github"
KINDS: tuple[str, ...] = (GITEA, GITHUB)

#: Where a GitHub forge with no ``url`` is asked.
GITHUB_API = "https://api.github.com"

#: The label that starts a run unless a forge names another.
DEFAULT_LABEL = "rfc"

#: Seconds between polls unless ``poll_every`` says otherwise.
DEFAULT_POLL_EVERY = 60

#: The one mode a credential file may have: read and write for its owner only.
CREDENTIAL_MODE = 0o600

SECTIONS: tuple[str, ...] = ("poll_every", "forges")
COMMON_FIELDS: tuple[str, ...] = (
    "name", "kind", "url", "repos", "operator_id", "label", "pool",
)
#: The fields each kind adds: its credentials, and for GitHub the App's id.
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    GITEA: ("token_file",),
    GITHUB: ("app_id", "key_file"),
}

# The entry a fault names when it is not in one forge.
FILE = "file"

_DURATION = re.compile(r"\A([1-9][0-9]*)([smh])\Z")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
_REPO = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")

_log = logging.getLogger(__name__)


class GatewayConfigError(SpecfloError):
    """A gateway configuration that cannot be used, with the entry and the field at fault."""

    def __init__(self, path: Path, entry: str, field: str, problem: str) -> None:
        super().__init__(f"{path}: {entry}: {field}: {problem}")
        self.path = path
        self.entry = entry
        self.field = field


@dataclass(frozen=True)
class Forge:
    """One forge the gateway watches. The credential fields are the absolute
    paths of their files, never what the files hold; the fields of the other
    kind are None."""

    name: str
    kind: str
    url: str
    repos: tuple[str, ...]
    operator_id: int
    pool: str
    label: str = DEFAULT_LABEL
    token_file: Path | None = None
    app_id: int | None = None
    key_file: Path | None = None


@dataclass(frozen=True)
class GatewayConfig:
    """What ``gateway.yaml`` declares. ``poll_every`` is in seconds."""

    path: Path | None
    forges: tuple[Forge, ...]
    poll_every: int = DEFAULT_POLL_EVERY


def config_file(root: Path) -> Path:
    """The gateway configuration file of the daemon root *root*."""
    return Path(root) / CONFIG_FILE


def open_gateway(root: Path) -> tuple[GatewayConfig | None, tuple[GatewayConfigError, ...]]:
    """The gateway configuration of the daemon root *root*, and the faults in its way.

    As :func:`load_gateway_config`, with each fault logged: the daemon serves
    without a gateway, and the log says which field kept it off.
    """
    found, errors = load_gateway_config(root)
    for error in errors:
        _log.warning("gateway configuration: %s", error)
    return found, tuple(errors)


def load_gateway_config(root: Path) -> tuple[GatewayConfig | None, list[GatewayConfigError]]:
    """The gateway configuration of the daemon root *root*, with every fault in it.

    A root with no ``gateway.yaml`` has neither. A file with any fault gives
    no configuration, and every fault found.
    """
    root = Path(root)
    path = config_file(root)
    if not path.exists() and not path.is_symlink():
        return None, []
    if not_a_file(path):
        return None, [GatewayConfigError(path, FILE, "file", "not a regular file.")]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, [GatewayConfigError(path, FILE, "file", f"cannot be read ({exc.strerror}).")]
    except UnicodeDecodeError:
        return None, [GatewayConfigError(path, FILE, "file", NOT_UTF8)]
    except yaml.YAMLError:
        return None, [GatewayConfigError(path, FILE, "file", "not valid YAML.")]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        problem = "not a mapping of " + ", ".join(SECTIONS) + "."
        return None, [GatewayConfigError(path, FILE, "file", problem)]

    errors: list[GatewayConfigError] = []
    for key in data:
        if key not in SECTIONS:
            errors.append(GatewayConfigError(
                path, FILE, str(key), "unknown key; expected one of " + ", ".join(SECTIONS) + "."
            ))
    poll_every = _poll_every(path, data.get("poll_every"), errors)

    entries = data.get("forges")
    if not isinstance(entries, list) or not entries:
        errors.append(GatewayConfigError(
            path, FILE, "forges", "must be a list of at least one forge."
        ))
        entries = []
    forges = []
    for position, fields in enumerate(entries, start=1):
        if not isinstance(fields, dict):
            errors.append(GatewayConfigError(
                path, f"forge {position}", "forges", "each forge must be a mapping of its fields."
            ))
            continue
        forges.append(_Forge(path, root, position, fields, errors).read())

    names = [forge.name for forge in forges if forge is not None]
    for name in dict.fromkeys(names):
        if names.count(name) > 1:
            errors.append(GatewayConfigError(
                path, f"forge '{name}'", "name",
                "declared more than once; give each forge a name of its own.",
            ))
    if errors:
        return None, errors
    return GatewayConfig(path, tuple(forges), poll_every), []


def read_credential(path: Path) -> str:
    """The secret in the credential file at *path*, without surrounding whitespace.

    The mode is checked again at each read, so a file opened up after the
    daemon started is refused. A fault names the file and never its content.
    """
    problem = _credential_problem(Path(path))
    if problem is not None:
        raise SpecfloError(f"{path}: {problem}")
    return Path(path).read_text(encoding="utf-8").strip()


def _credential_problem(path: Path) -> str | None:
    """What keeps the file at *path* from serving as a credential, or None."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return "no such file in the daemon root."
    except OSError as exc:
        return f"cannot be read ({exc.strerror})."
    if not stat.S_ISREG(status.st_mode):
        return "not a regular file."
    mode = stat.S_IMODE(status.st_mode)
    if mode != CREDENTIAL_MODE:
        return f"must have mode 0600, not {mode:04o}; run chmod 600 on it."
    return None


def _poll_every(path: Path, value: object, errors: list[GatewayConfigError]) -> int:
    if value is None:
        return DEFAULT_POLL_EVERY
    match = _DURATION.match(value) if isinstance(value, str) else None
    if match is None:
        errors.append(GatewayConfigError(
            path, FILE, "poll_every",
            "must be a whole number of at least 1 and a unit, s, m or h, such as '1m'.",
        ))
        return DEFAULT_POLL_EVERY
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


class _Forge:
    """One forge entry being read, collecting its faults."""

    def __init__(
        self, path: Path, root: Path, position: int, fields: dict,
        errors: list[GatewayConfigError],
    ) -> None:
        self.path = path
        self.root = root
        self.fields = fields
        self.errors = errors
        name = fields.get("name")
        kind = fields.get("kind")
        label = name if isinstance(name, str) and name else kind
        self.entry = f"forge '{label}'" if isinstance(label, str) and label else f"forge {position}"

    def fault(self, field: str, problem: str) -> None:
        self.errors.append(GatewayConfigError(self.path, self.entry, field, problem))

    def read(self) -> Forge | None:
        before = len(self.errors)
        kind = self.fields.get("kind")
        if kind not in KINDS:
            self.fault("kind", "must be one of " + ", ".join(KINDS) + f", not {kind!r}.")
            known = COMMON_FIELDS + tuple(f for fields in KIND_FIELDS.values() for f in fields)
        else:
            known = COMMON_FIELDS + KIND_FIELDS[kind]
        for key in self.fields:
            if key not in known:
                self.fault(str(key), "unknown field; expected one of " + ", ".join(known) + ".")

        name = self.fields.get("name", kind)
        if not isinstance(name, str) or not name.strip():
            self.fault("name", "must be a name.")
        url = self._url(kind)
        repos = self._repos()
        operator_id = self._positive("operator_id", "the operator's user id on the forge")
        pool = self._text("pool", None, "the pool to lease research agents from")
        label = self._text("label", DEFAULT_LABEL, "the label that starts a run")
        token_file = app_id = key_file = None
        if kind == GITEA:
            token_file = self._credential("token_file", "the bot's token")
        elif kind == GITHUB:
            app_id = self._positive("app_id", "the GitHub App's id")
            key_file = self._credential("key_file", "the GitHub App's private key")
        if len(self.errors) > before:
            return None
        return Forge(
            name=name.strip(), kind=kind, url=url, repos=repos, operator_id=operator_id,
            pool=pool, label=label, token_file=token_file, app_id=app_id, key_file=key_file,
        )

    def _url(self, kind: object) -> str:
        value = self.fields.get("url")
        if value is None and kind == GITHUB:
            return GITHUB_API
        if isinstance(value, str):
            parts = urlsplit(value.strip())
            if parts.scheme in ("http", "https") and parts.hostname:
                return value.strip().rstrip("/")
        self.fault("url", "must be the forge's http or https base URL.")
        return ""

    def _repos(self) -> tuple[str, ...]:
        value = self.fields.get("repos")
        if (
            not isinstance(value, list) or not value
            or not all(isinstance(r, str) and _REPO.match(r) for r in value)
        ):
            self.fault("repos", "must list at least one repo, each written owner/name.")
            return ()
        return tuple(value)

    def _positive(self, field: str, what: str) -> int:
        value = self.fields.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            self.fault(field, f"required; {what}, a whole number of at least 1.")
            return 0
        return value

    def _text(self, field: str, default: str | None, what: str) -> str:
        value = self.fields.get(field, default)
        if not isinstance(value, str) or not value.strip():
            self.fault(field, f"required; {what}.")
            return ""
        return value.strip()

    def _credential(self, field: str, what: str) -> Path | None:
        value = self.fields.get(field)
        if not isinstance(value, str) or not value.strip():
            self.fault(field, f"required; the name of the file in the daemon root with {what}.")
            return None
        name = Path(value.strip())
        if name.is_absolute() or ".." in name.parts:
            self.fault(field, f"{value!r} must be a file name under the daemon root.")
            return None
        location = self.root / name
        problem = _credential_problem(location)
        if problem is not None:
            self.fault(field, f"{location}: {problem}")
            return None
        return location
