"""The ``specflo research`` sub-app: the commands of a research project.

A research project is local only, so these commands call the research package
directly, as the followup verbs do, and nothing of them crosses the wire.
They work on the active project when it has a research document, whatever
its level: a research project moved up to full keeps its research files.
Each group below is a place for the later ledger commands to mount beside.
"""

from __future__ import annotations

from pathlib import Path

import typer

from . import config
from . import guide as guide_module
from .errors import SpecfloError
from .research import document, findings, ledger, questions

research_app = typer.Typer(
    help="Work a research project: its usual user, destination and sessions."
)
user_app = typer.Typer(help="The usual user: who usually sits at the keyboard.")
destination_app = typer.Typer(
    help="Where the research is headed: build, inform, learn or unknown."
)
session_app = typer.Typer(help="The research sessions.")
research_app.add_typer(user_app, name="user")
research_app.add_typer(destination_app, name="destination")
research_app.add_typer(session_app, name="session")


def _die(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def research_project() -> tuple[Path, config.SpecfloConfig, str]:
    """The checkout root, its config and the active project's slug.

    Refused, with exit 1, outside a checkout, with no active project, and
    when the active project has no research document: the commands test for
    the document, not the level, so a project moved up from research keeps
    working.
    """
    root = config.find_root(Path.cwd())
    if root is None:
        raise _die("Not a specflo project. Run `specflo init` first.")
    cfg = config.load_config(root)
    slug = cfg.active_project
    if slug is None:
        raise _die(guide_module.NO_ACTIVE_PROJECT_MESSAGE)
    try:
        present = document.has_document(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if not present:
        raise _die(
            f"Project {slug!r} has no research document ({document.RESEARCH_FILENAME}):"
            " the research commands work on a project started with"
            " `specflo new <name> --level research`."
        )
    return root, cfg, slug


@user_app.command(
    "set",
    epilog='Example: specflo research user set --text "A broker with twenty years in leasing."',
)
def user_set(
    text: str | None = typer.Option(
        None, "--text", help="The description, in the user's own words."
    ),
    file: Path | None = typer.Option(
        None, "--file", metavar="<path>", help="A file holding the description."
    ),
) -> None:
    """Record who usually sits at the keyboard, in their own words.

    A later call replaces the description and the log keeps the previous text.
    Give the text with exactly one of --text or --file.
    """
    if (text is None) == (file is None):
        raise _die("Give the description with exactly one of --text or --file.")
    if file is not None:
        try:
            text = file.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise _die(f"Cannot read {file}: {exc}.")
    root, cfg, slug = research_project()
    try:
        previous = document.set_user(root, cfg, slug, text)
    except SpecfloError as exc:
        raise _die(str(exc))
    kept = " The previous text is kept in the log." if previous else ""
    typer.echo(f"Recorded the usual user in {slug}/research.{kept}")


question_app = typer.Typer(
    help="The research questions: what is still to find out, and who answers it."
)
research_app.add_typer(question_app, name="question")


@question_app.command(
    "add",
    epilog=(
        'Example: specflo research question add --text "Which market first?" --by developer'
        ' --option Toronto --option Calgary --default Toronto --why "the user lives there"'
    ),
)
def question_add(
    text: str = typer.Option(..., "--text", help="The question (one line)."),
    by: str = typer.Option(
        ..., "--by", metavar="user|developer", help="Who answers it."
    ),
    option: list[str] = typer.Option(
        [], "--option", metavar="<text>", help="A possible answer; repeat for each."
    ),
    default: str | None = typer.Option(
        None, "--default", metavar="<text>",
        help="The answer the work goes on with when nobody answers; a developer question needs one.",
    ),
    why: str | None = typer.Option(
        None, "--why", metavar="<text>", help="Why the question matters (one line)."
    ),
    supersedes: str | None = typer.Option(
        None, "--supersedes", metavar="Q-NN", help="The question this one replaces."
    ),
) -> None:
    """Record an open question: who answers it, its options, default and why.

    A developer question without a default is refused and nothing is written.
    With --supersedes the old question is marked as replaced by the new one.
    """
    root, cfg, slug = research_project()
    try:
        entry = questions.add_question(
            root, cfg, slug, text, by, options=option, default=default, why=why,
            supersedes=supersedes,
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    replaced = f" It supersedes {entry.supersedes}." if entry.supersedes else ""
    typer.echo(
        f"Recorded {entry.id} in {slug}/research/{ledger.QUESTIONS.filename}"
        f" (open, the {by} answers).{replaced}"
    )


@question_app.command(
    "resolve",
    epilog='Example: specflo research question resolve Q-01 --resolution "Toronto first." --by Ana',
)
def question_resolve(
    question_id: str = typer.Argument(..., metavar="Q-NN", help="The question to resolve."),
    resolution: str = typer.Option(..., "--resolution", help="The answer (one line)."),
    by: str | None = typer.Option(
        None, "--by", metavar="<name>", help="Who resolved it; the usual user when not given."
    ),
) -> None:
    """Resolve an open question: record the resolution and who gave it.

    An unknown, superseded or already resolved question is refused and nothing
    is written.
    """
    root, cfg, slug = research_project()
    try:
        entry = questions.resolve_question(root, cfg, slug, question_id, resolution, by=by)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(
        f"Resolved {entry.id} in {slug}/research/{ledger.QUESTIONS.filename}"
        f" (by {entry.get(questions.RESOLVED_BY_FIELD)})."
    )


@destination_app.command(
    "set",
    epilog='Example: specflo research destination set build --reason "the user wants a tool"',
)
def destination_set(
    value: str = typer.Argument(
        ..., metavar="build|inform|learn|unknown", help="Where the research is headed."
    ),
    reason: str = typer.Option(..., "--reason", help="Why it changed (one line)."),
) -> None:
    """Record where the research is headed and why.

    The log keeps the old value, the new one and the reason. Any other value,
    or no reason, is refused and nothing changes.
    """
    root, cfg, slug = research_project()
    try:
        old, new = document.set_destination(root, cfg, slug, value, reason)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Destination {old} -> {new} in {slug}/research.")


finding_app = typer.Typer(help="The findings ledger: one fact or inference per entry, with its citations.")
research_app.add_typer(finding_app, name="finding")


@finding_app.command(
    "add",
    epilog=(
        'Example: specflo research finding add --text "Rent rose 4% in 2025." --kind fact'
        " --status supported --cites S-01:p3 --cites S-02"
    ),
)
def finding_add(
    text: str = typer.Option(..., "--text", help="The claim, on one line."),
    kind: str = typer.Option(
        ..., "--kind", metavar="fact|inference",
        help="A fact rests on at least one citation; an inference may stand on none.",
    ),
    status: str = typer.Option(
        ..., "--status", metavar="supported|contradicted|unsure",
        help="What support the claim has from its sources.",
    ),
    cites: list[str] = typer.Option(
        None, "--cites", metavar="S-NN[:pN]",
        help="A source of the sources ledger, or a page of a PDF source (repeatable).",
    ),
    supersedes: str | None = typer.Option(
        None, "--supersedes", metavar="RF-NN", help="The finding this one replaces."
    ),
) -> None:
    """Record a finding: a fact or an inference, its status and the sources it cites.

    Each citation is checked before anything is written: the source must be in
    the sources ledger, and a page is accepted only on a PDF and within its
    page count. A fact with no citation is refused. With --supersedes the old
    finding is marked as replaced by the new one and keeps its own status.
    """
    root, cfg, slug = research_project()
    try:
        entry = findings.add_finding(
            root, cfg, slug, text, kind, status, cites=cites or [], supersedes=supersedes
        )
    except SpecfloError as exc:
        raise _die(str(exc))
    cited = ", ".join(entry.all(findings.CITES_FIELD)) or "none"
    replaced = f", supersedes {entry.supersedes}" if entry.supersedes else ""
    typer.echo(
        f"Recorded {entry.id} ({entry.get(findings.KIND_FIELD)}, {entry.status}; cites: {cited}{replaced})"
        f" in {slug}/research/{ledger.FINDINGS.filename}."
    )


@session_app.command(
    "close",
    epilog=(
        'Example: specflo research session close --summary "Read the lease act"'
        ' --next "Ask about renewals"'
    ),
)
def session_close(
    summary: str = typer.Option(..., "--summary", help="What the session did (one line)."),
    next_steps: str = typer.Option(
        ..., "--next", help="What the next session starts with (one line)."
    ),
    present: str | None = typer.Option(
        None, "--present", metavar="<who>",
        help="Who was present; the usual user when not given.",
    ),
) -> None:
    """End a session: one dated log entry with the summary, the next steps and who was present."""
    root, cfg, slug = research_project()
    try:
        session = document.close_session(root, cfg, slug, summary, next_steps, present=present)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(f"Closed the session of {session.date} in {slug}/research (present: {session.present}).")
