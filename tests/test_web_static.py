"""The web UI is built by nothing: templates, an assets directory, and no node toolchain.

The daemon renders its pages from templates in the package and serves the
browser scripts they need from the package's assets directory, vendored as
they are. Nothing in the repository builds JavaScript: no manifest, no lock
file, no bundler, no TypeScript source, except the pi extension, which is
pi's own extension format and is loaded by pi from its TypeScript entry
without a build step of its own.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.daemon import auth, web
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products
from specflo.daemon.workitems import WorkItems
from specflo.service.local import LocalProjectService

REPO_ROOT = Path(__file__).resolve().parent.parent

# pi loads its extensions from a package.json plus TypeScript sources; that
# directory is pi's contract, not a build of ours, and is the one exemption.
PI_EXTENSION = "src/specflo/extension/"

NODE_FILES = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lockb", "bun.lock", "tsconfig.json", "jsconfig.json",
    ".babelrc", "babel.config.js", ".nvmrc", ".npmrc",
}
NODE_PREFIXES = ("vite.config.", "webpack.config.", "rollup.config.", "esbuild.", "postcss.config.", "tailwind.config.")
# Sources that need compiling before a browser or node can run them. Plain
# .js/.mjs/.cjs files run as they are (the vendored htmx, a node test double).
SOURCE_SUFFIXES = (".ts", ".tsx", ".jsx")


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    )
    return [Path(line) for line in out.stdout.split()]


def looks_like_node_toolchain(path: Path) -> bool:
    name = path.name
    return (
        name in NODE_FILES
        or name.startswith(NODE_PREFIXES)
        or "node_modules" in path.parts
        or name.endswith(SOURCE_SUFFIXES)
    )


# --- the source tree ------------------------------------------------------------


def test_the_web_ui_has_templates_and_an_assets_directory():
    assert web.TEMPLATES_DIR.is_dir()
    assert web.ASSETS_DIR.is_dir()
    assert {p.name for p in web.TEMPLATES_DIR.glob("*.html")} >= {
        "base.html", "signin.html", "products.html", "product.html", "project.html"
    }
    assert (web.ASSETS_DIR / "htmx.min.js").is_file()


def test_no_node_toolchain_file_outside_the_pi_extension():
    offenders = [
        str(path)
        for path in tracked_files()
        if looks_like_node_toolchain(path) and not str(path).startswith(PI_EXTENSION)
    ]
    assert offenders == [], offenders


def test_the_only_javascript_the_package_ships_is_vendored_in_the_assets_directory():
    scripts = [
        str(path)
        for path in tracked_files()
        if path.suffix in (".js", ".mjs", ".cjs")
        and str(path).startswith("src/")
        and not str(path).startswith(PI_EXTENSION)
    ]
    assets = str(web.ASSETS_DIR.relative_to(REPO_ROOT)) + "/"
    assert scripts, "no browser script is vendored at all"
    assert all(script.startswith(assets) for script in scripts), scripts


def test_the_pi_extension_declares_no_build_step():
    manifest = json.loads((REPO_ROOT / PI_EXTENSION / "package.json").read_text())
    scripts = manifest.get("scripts", {})
    assert not {"build", "bundle", "compile", "prepare", "prepublish"} & set(scripts), scripts
    assert manifest["pi"]["extensions"][0].endswith(".ts")


# --- the rendered pages ---------------------------------------------------------


@pytest.fixture
def pages(tmp_path):
    """Every page the web UI renders, signed in, over a daemon with one of everything."""
    root = daemon.prepare_root(tmp_path / "daemon")
    with store_module.open_store(root) as store:
        Products(store).add("Thing", slug="thing", today="2026-09-06")
        Products(store).set_vision("thing", "Ship the thing.\n")
        WorkItems(store).add("thing", "Fix the login", today="2026-09-06")
        service = LocalProjectService(root, config.load_config(root))
        WorkItems(store).spawn(1, service, name="Login fix")
    client = TestClient(create_app(root), follow_redirects=False)
    signin = client.get(web.SIGNIN_PATH)
    token = auth.mint_token(root, "developer")
    assert client.post(web.SIGNIN_PATH, data={"identity": "developer", "token": token}).status_code == 303
    rendered = {
        web.SIGNIN_PATH: signin,
        web.HOME_PATH: client.get(web.HOME_PATH),
        web.PRODUCT_PATH.format(slug="thing"): client.get(web.PRODUCT_PATH.format(slug="thing")),
        web.PROJECT_PATH.format(slug="login-fix"): client.get(web.PROJECT_PATH.format(slug="login-fix")),
    }
    for path, response in rendered.items():
        assert response.status_code == 200, path
    return client, rendered


def script_tags(html):
    return re.findall(r"<script\b[^>]*>", html)


def test_every_script_tag_on_every_page_points_at_the_assets_directory(pages):
    client, rendered = pages
    for path, response in rendered.items():
        tags = script_tags(response.text)
        assert tags, f"{path} ships no script"
        for tag in tags:
            src = re.search(r'\ssrc="([^"]*)"', tag)
            assert src, f"{path} has an inline script: {tag}"
            assert src.group(1).startswith(web.ASSETS_PATH + "/"), (path, tag)
            served = client.get(src.group(1))
            assert served.status_code == 200, (path, src.group(1))
            assert (web.ASSETS_DIR / src.group(1).removeprefix(web.ASSETS_PATH + "/")).is_file()


def test_no_page_pulls_a_script_or_stylesheet_from_another_host(pages):
    _, rendered = pages
    for path, response in rendered.items():
        for tag in re.findall(r"<(?:script|link)\b[^>]*>", response.text):
            assert "http://" not in tag and "https://" not in tag and "//" not in tag, (path, tag)
