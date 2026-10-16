"""The ``specflo research`` sub-app: the commands of a research project.

A research project is local only, so these commands call the research package
directly, as the followup verbs do, and nothing of them crosses the wire.
They work on the active project when it has a research document, whatever
its level: a research project moved up to full keeps its research files.
Each group below is a place for the later ledger commands to mount beside.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from . import config
from . import guide as guide_module
from .errors import SpecfloError
from .research import (
    askdev,
    document,
    findings,
    glossary,
    ledger,
    questions,
    recap,
    sources,
    spinoff,
)
from .research import ingest as ingest_module
from .research import lint as lint_module

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


@research_app.command("ingest", epilog="Example: specflo research ingest notes/lease-act.md")
def ingest(
    file: Path = typer.Argument(
        ...,
        metavar="<file>",
        help=(
            "A .md, .txt, .pdf or .docx file to keep as a document source;"
            # Rich reads [docs] as a style tag unless the bracket is escaped.
            " PDF and DOCX need the specflo\\[docs] extra."
        ),
    ),
) -> None:
    """Keep a file as a document source (S-NN).

    The original is stored byte for byte under research/sources/S-NN/ with its
    text beside it, and the sources ledger records the file's name, format,
    sha256 and date. A file whose bytes a source already holds is refused,
    naming that source, and nothing is written.
    """
    root, cfg, slug = research_project()
    try:
        hint = sources.egress_hint(root, cfg, slug)
        entry = ingest_module.ingest_file(root, cfg, slug, file)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(
        f"Ingested {entry.title} as {entry.id}: {slug}/{entry.get(sources.ORIGINAL_FIELD)}"
        f" (original), {slug}/{entry.get(sources.TEXT_FIELD)} (text)."
    )
    _print_egress_hint(hint)


# --- the sources whose text the user gives ----------------------------------------------

source_app = typer.Typer(help="The sources ledger: interview, web and repo sources.")
research_app.add_typer(source_app, name="source")


def _print_egress_hint(hint: str | None) -> None:
    """Print the egress suggestion when the source just added was the project's first."""
    if hint:
        typer.echo(hint)


def _read_source_text(file: Path | None, use_stdin: bool) -> str:
    """The text of a new source, from the file or standard input; refused when not UTF-8."""
    if use_stdin:
        data, what = sys.stdin.buffer.read(), "standard input"
    else:
        if not file.is_file():
            if file.exists():
                raise SpecfloError(f"Cannot read {file}: it is not a file.")
            raise SpecfloError(f"No file at {file}.")
        try:
            data, what = file.read_bytes(), str(file)
        except OSError as exc:
            raise SpecfloError(f"Cannot read {file}: {exc}.") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecfloError(
            f"Cannot read {what}: it is not valid UTF-8 (at byte {exc.start})."
            " The text of a source is stored as UTF-8; convert it first."
        ) from exc


@source_app.command(
    "add",
    epilog=(
        'Example: specflo research source add --kind interview --title "Ana on renewals"'
        " --file notes/ana.md"
    ),
)
def source_add(
    kind: str = typer.Option(
        ..., "--kind", metavar="interview|web|repo",
        help="What the source is; a document comes in through `research ingest`.",
    ),
    title: str = typer.Option(..., "--title", help="The source's title (one line)."),
    file: Path | None = typer.Option(
        None, "--file", metavar="<path>", help="A UTF-8 text file holding the source's text."
    ),
    use_stdin: bool = typer.Option(
        False, "--stdin", help="Read the source's text from standard input."
    ),
    url: str | None = typer.Option(
        None, "--url", metavar="<url>",
        help="Where a web page or repository was fetched from; those two need it.",
    ),
) -> None:
    """Record an interview, web or repo source (S-NN) with its text.

    The text is stored as given under research/sources/S-NN/text.md and the
    sources ledger records the kind, date, title and, for web and repo, the
    URL. Give the text with exactly one of --file or --stdin. A web or repo
    source without --url, or an interview with one, is refused and nothing is
    written. The first source of a project with no egress pin prints one
    suggestion to pin a class; it sets no pin.
    """
    if (file is None) == (not use_stdin):
        raise _die("Give the text with exactly one of --file or --stdin.")
    root, cfg, slug = research_project()
    try:
        text = _read_source_text(file, use_stdin)
        hint = sources.egress_hint(root, cfg, slug)
        entry = sources.add_text_source(root, cfg, slug, kind, title, text, url=url)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(
        f"Recorded {entry.title!r} as {entry.id} ({entry.get(sources.KIND_FIELD)}):"
        f" {slug}/{entry.get(sources.TEXT_FIELD)} (text)."
    )
    _print_egress_hint(hint)


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


@research_app.command("ask-dev", epilog="Example: specflo research ask-dev")
def ask_dev() -> None:
    """Print the open developer questions as email text: a subject line and a plain body.

    Each question comes with its ID, options, default and why; user questions
    stay out. Nothing is written and nothing is sent: paste the text into your
    own mail client. With no open developer question it says so in one line.
    """
    root, cfg, slug = research_project()
    try:
        subject, body = askdev.render_ask_dev(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if not subject:
        typer.echo(body)
        return
    typer.echo(f"Subject: {subject}")
    typer.echo()
    typer.echo(body)


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


# --- listing a ledger ------------------------------------------------------------------

# The ledger each listing name stands for: the glossary is listed as "terms".
LISTED_LEDGERS = {
    "sources": ledger.SOURCES,
    "questions": ledger.QUESTIONS,
    "findings": ledger.FINDINGS,
    "terms": ledger.GLOSSARY,
}


def _entry_heading(entry: ledger.Entry) -> str:
    """The first line of a listed entry: its ID and title, with its superseded mark."""
    heading = f"{entry.id} - {entry.title}" if entry.title else entry.id
    if entry.superseded_by is not None:
        heading += f" [superseded by {entry.superseded_by}]"
    return heading


def _entry_json(entry: ledger.Entry) -> dict:
    """One listed entry as JSON: a repeated field is a list; a term's title is the term."""
    fields: dict[str, str | list[str]] = {}
    for name, value in entry.fields:
        if name not in fields:
            fields[name] = value
        elif isinstance(fields[name], list):
            fields[name].append(value)
        else:
            fields[name] = [fields[name], value]
    return {
        "id": entry.id,
        "title": entry.title or entry.id,
        "status": entry.status,
        "supersedes": entry.supersedes,
        "superseded_by": entry.superseded_by,
        "active": entry.active,
        "fields": fields,
    }


@research_app.command(
    "list",
    epilog="Example: specflo research list questions --open",
)
def research_list(
    name: str = typer.Argument(
        ..., metavar="sources|questions|findings|terms", help="The ledger to list."
    ),
    open_only: bool = typer.Option(
        False, "--open", help="Questions only: keep the open ones, not resolved or superseded."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List a ledger: one block per entry with its ID, title and every field.

    A superseded entry is marked on its first line and keeps its fields. The
    glossary is listed as terms, each keyed by its term. --open applies to the
    questions only. The command reads the ledger and changes nothing.
    """
    chosen = LISTED_LEDGERS.get(name)
    if chosen is None:
        raise _die(
            f"Unknown ledger {name!r}: list one of " + ", ".join(LISTED_LEDGERS) + "."
        )
    if open_only and chosen is not ledger.QUESTIONS:
        raise _die(
            f"--open applies to questions only: `research list questions --open` keeps the"
            f" open ones; the {name} ledger has no open state."
        )
    root, cfg, slug = research_project()
    try:
        entries = chosen.entries(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if open_only:
        entries = [entry for entry in entries if questions.is_open(entry)]
    if json_output:
        typer.echo(json.dumps([_entry_json(entry) for entry in entries]))
        return
    if not entries:
        what = f"open {name}" if open_only else name
        typer.echo(f"No {what} in {slug}/{document.RESEARCH_DIRNAME}/{chosen.filename}.")
        return
    for entry in entries:
        typer.echo(_entry_heading(entry))
        for field, value in entry.fields:
            typer.echo(f"  {field}: {value}")


# --- checking the findings against their sources ------------------------------------------


@research_app.command("lint", epilog="Example: specflo research lint")
def research_lint() -> None:
    """Check each finding's figures against the text of the sources it cites.

    A figure is a number, a date or a double-quoted string of the finding's
    text; it passes when it appears word for word in the stored text of at
    least one cited source, within the cited page when the citation names
    one. A number is looked for with and without thousands separators, so
    1000 is found in a source that writes 1,000 and 1,000 in one that writes
    1000, but only as a whole number: 1000 is not found in 10000 or 1000.5.
    Each missing figure is one line naming the finding, the figure and
    the citations. Exits 1 with any problem and 0 without. A finding with no
    citation and a superseded finding are skipped. The command reads only and
    nothing else depends on it passing.
    """
    root, cfg, slug = research_project()
    try:
        entries = lint_module.cited_findings(root, cfg, slug)
        problems = lint_module.lint(root, cfg, slug, entries)
    except SpecfloError as exc:
        raise _die(str(exc))
    typer.echo(lint_module.render(problems, len(entries)))
    if problems:
        raise typer.Exit(code=1)


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


# --- the glossary ---------------------------------------------------------------

term_app = typer.Typer(help="The glossary: the domain's terms in the user's own words.")
research_app.add_typer(term_app, name="term")


@term_app.command(
    "set",
    epilog=(
        'Example: specflo research term set --term "Cap rate"'
        ' --definition "Net operating income over the price" --avoid yield'
    ),
)
def term_set(
    term: str = typer.Option(..., "--term", help="The term, as the user says it (one line)."),
    definition: str = typer.Option(
        ..., "--definition", help="What the term means, in the user's own words (one line)."
    ),
    avoid: list[str] = typer.Option(
        None, "--avoid", metavar="<word>", help="A word to avoid for this term (repeatable)."
    ),
) -> None:
    """Set a glossary term with its definition and the words to avoid.

    Setting a term again replaces its definition and words to avoid in place,
    and the log keeps the previous definition. Nothing deletes a term.
    """
    root, cfg, slug = research_project()
    try:
        previous = glossary.get_term(root, cfg, slug, term)
        entry = glossary.set_term(root, cfg, slug, term, definition, avoid or ())
    except SpecfloError as exc:
        raise _die(str(exc))
    kept = " The previous definition is kept in the log." if previous else ""
    typer.echo(
        f"Recorded the term {entry.id!r} in {slug}/{document.RESEARCH_DIRNAME}/"
        f"{glossary.GLOSSARY.filename}.{kept}"
    )


# --- the recap -----------------------------------------------------------------


@research_app.command("recap", epilog="Example: specflo research recap --json")
def research_recap(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print the session recap: destination and why, the usual user, the last session,
    the open questions by who answers them, the ledger counts and the privacy rule.

    The recap reads every research file and writes none. It ends with the one
    rule for an outgoing web search, so an agent sees it before it searches.
    """
    root, cfg, slug = research_project()
    try:
        payload = recap.build_recap(root, cfg, slug)
    except SpecfloError as exc:
        raise _die(str(exc))
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(recap.render_recap(payload), nl=False)


# --- spin-offs -----------------------------------------------------------------


@research_app.command(
    "spinoff",
    epilog="Example: specflo research spinoff 'Lease tool' --level fast --use RF-01,Q-02",
)
def research_spinoff(
    name: str = typer.Argument(
        ..., metavar="<name>", help="The new project's name; slugified into its directory name."
    ),
    level: str = typer.Option(
        ..., "--level", metavar="|".join(spinoff.SPINOFF_LEVELS),
        help="How much ceremony the new project gets; research and harden are refused.",
    ),
    use: list[str] = typer.Option(
        ..., "--use", metavar="<ID>[,<ID>]...",
        help="The entries to hand off, by ID: sources (S-NN), questions (Q-NN) and findings"
             " (RF-NN), comma-separated; repeatable.",
    ),
) -> None:
    """Spin a seeded project off the research.

    The new project is created as `specflo new <name> --level <level>` creates
    one and becomes active; its handoff.md holds each named entry with its ID
    and full text, the research project's name and the date. The research
    project keeps its phase and status and its log names the spin-off. An
    unknown ID, a glossary term, a repeated ID or a bad level is refused before
    anything is created.
    """
    root, cfg, slug = research_project()
    try:
        result = spinoff.spin_off(root, cfg, slug, name, level, spinoff.split_ids(use))
    except SpecfloError as exc:
        raise _die(str(exc))
    ids = ", ".join(result.entries)
    typer.echo(f"Created project '{result.slug}' (now active). Phase: {result.phase}.")
    typer.echo(
        f"Scaffolded {result.slug}/{result.scaffolded.stem} and"
        f" {result.slug}/{result.handoff_path.stem} ({ids}) (ready to work)."
    )
    typer.echo(
        f"Research project '{slug}' stays as it was and is no longer active:"
        f" `specflo switch {slug}` returns to it."
    )
