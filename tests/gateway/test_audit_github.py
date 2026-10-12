"""No secret of a GitHub forge goes anywhere a run writes or hands out.

The checks of :mod:`tests.gateway.test_audit`, for a run on a GitHub forge:
with the App's private key in the daemon root, neither the key, the JWTs it
signed nor the installation token GitHub handed out shows in the daemon's
log, the audit file, a comment the bot posted, or what the member is handed:
its environment, its prompt and the files of its working dir. The clone is
the one place the token goes, as git's credential.
"""

import asyncio
import dataclasses
import json
import logging
from pathlib import Path

from specflo.daemon import routes
from specflo.gateway import config, github, service, store

from .stub_forge import APP_ID, StubGitHub, app_key
from .test_audit import OPERATOR, REPO, StubPool, _run_records, _secret_file, fails, reports

INSTALLATION_TOKEN = "ghs_fake-installation-token-5ecre7-d4e5f6"


def test_no_github_token_or_key_reaches_the_log_the_audit_the_comments_or_the_member(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    root = tmp_path / "root"
    root.mkdir()
    stub = StubGitHub(token=INSTALLATION_TOKEN)
    pem = app_key()[0]
    forge = config.Forge(
        name="hub", kind=config.GITHUB, url=stub.base_url, repos=(REPO,),
        operator_id=OPERATOR, pool="rfc-research", app_id=APP_ID,
        key_file=_secret_file(root, "github.pem", pem),
    )
    kind = dataclasses.replace(
        service.KINDS[config.GITHUB],
        client=lambda forge: github.GitHubClient(forge, transport=stub.transport()),
    )
    clone_tokens = []

    async def clone(url, destination, *, token=None, **options):
        clone_tokens.append(token)
        Path(destination).mkdir()

    stub.add_issue(REPO, 1)
    stub.add_issue(REPO, 2)
    start = stub.now
    for number in (1, 2):
        stub.tick()
        stub.label(REPO, number, forge.label, OPERATOR)
    done = iter([reports, fails])

    async def agent(cwd):
        await next(done)(cwd)

    pool = StubPool(agent)

    async def main():
        gateway = service.GatewayService(
            store.open_store(root, now=start), pool, kinds={config.GITHUB: kind},
            clone=clone, clock=lambda: stub.now, lease_retry=0.01,
        )
        try:
            await gateway.poll(config.GatewayConfig(path=None, forges=(forge,), poll_every=60))
            await gateway.join()
        finally:
            await gateway.aclose()

    asyncio.run(main())

    # The check bites: both runs happened, the member was handed something,
    # the forge was reached with the token and the clone was given it.
    assert len(_run_records(root)) == 2
    assert len(pool.handed) == 4
    assert any(INSTALLATION_TOKEN in str(seen.headers) for seen in stub.requests)
    assert clone_tokens == [INSTALLATION_TOKEN, INSTALLATION_TOKEN]
    jwts = [
        seen.headers["authorization"].partition(" ")[2] for seen in stub.requests
        if seen.headers.get("authorization", "").startswith("Bearer ")
    ]
    assert jwts
    key_lines = [line for line in pem.splitlines() if line and not line.startswith("-----")]
    secrets = [INSTALLATION_TOKEN, *jwts, *key_lines]

    log = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    audit = (root / routes.AUDIT_FILENAME).read_text(encoding="utf-8")
    bodies = [
        json.dumps(seen.body) for seen in stub.requests
        if seen.method in ("POST", "PATCH") and "/comments" in seen.path
    ]
    assert bodies
    handed = json.dumps(pool.handed)
    for secret in secrets:
        assert secret not in log
        assert secret not in audit
        assert all(secret not in body for body in bodies)
        assert secret not in handed
