# specflo

[![PyPI](https://img.shields.io/pypi/v/specflo)](https://pypi.org/project/specflo/)
[![Python versions](https://img.shields.io/pypi/pyversions/specflo)](https://pypi.org/project/specflo/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](https://github.com/TacoTakumi/specflo/blob/main/LICENSE)

**Spec-driven software engineering for coding agents.** specflo is a Python CLI
plus a set of skills and hooks that give an AI agent a disciplined
brainstorm -> spec -> plan -> execute loop, with every artifact (specs, plans,
decisions, tasks) written to plain markdown on disk instead of living in the
model's context window.

- **State lives in files, not in the model's head.** Phase state, validation
  gates, and one-task-at-a-time focus live in the CLI and on disk. That makes
  agentic development more reliable, and it makes real development practical
  with capable but smaller *local* models such as Qwen3.6 27B: the agent only
  has to reason about the next small, well-scoped, validated step, not hold an
  entire project in context.
- **Multi-project by design.** specflo tracks many projects in one repo with a
  single active project and switch-anytime, so a monorepo can carry several
  concurrent efforts without them stepping on each other. Each project keeps
  its own phase, artifacts, and resume checkpoint.
- **Pluggable into any harness that can run a CLI and read markdown.** Claude
  Code and [pi] are integrated out of the box (skills, session-start hook, a
  bundled pi extension); opencode and Hermes get the skills; anything else can
  drive the plain CLI.

specflo is built with itself: every feature since v0.1 has gone through its own
brainstorm -> spec -> plan -> execute pipeline. The [CHANGELOG](https://github.com/TacoTakumi/specflo/blob/main/CHANGELOG.md)
is the development history. Pre-1.0, interfaces may still move; breaking
changes are called out explicitly in the changelog.

**Three surfaces are a heavy work in progress and change constantly: pi
subagents (`specflo agent`), daemon hosting (`specflo serve`) and the agent
pool (`specflo lease`, `specflo console`).** pi subagents are written up below,
and the daemon and the pool in
[DAEMON.md](https://github.com/TacoTakumi/specflo/blob/main/DAEMON.md), because
they work and are being used, not because they are settled. Expect their
commands, flags, on-disk formats and behaviour to move between releases, with
no migration path.

## Quick start

Requires Python 3.12+. Install from PyPI:

```bash
# With uv
uv tool install specflo

# Or with pipx
pipx install specflo

# Or with pip
pip install specflo
```

To bring PDF and Word documents into a [research project](#research-projects),
add the optional `docs` extra: `uv tool install 'specflo[docs]'` (or
`pip install 'specflo[docs]'`). Markdown and plain-text files need nothing more.

Then set up the repo you want to work in:

```bash
# Scaffold the .specflo/ config and the projects dir
specflo init

# Install the workflow skills into your agent harness(es)
specflo skills install

# Claude Code: session-resume wiring (recommended)
specflo hook install

# pi: the same session-resume wiring, as a pi extension
specflo extension install
```

The hook and extension commands are per-harness alternatives - run the one that
matches your agent, or neither if it is some other harness. Then add the specflo
note to your agent's memory file, as [Using specflo with your agent](#using-specflo-with-your-agent)
describes, and check the setup with `specflo doctor`. It names the command that
fixes each problem it finds.

### Your first project

```bash
# A full-level project: brainstorm, spec, plan and execute
specflo new "my first project"

# Or, for smaller work, a quick (one check) or fast (3 to 7 tasks) project
# specflo new "my first project" --level quick
# specflo new "my first project" --level fast

# Or, to research an idea over several sessions before anything is built
# specflo new "my first project" --level research
```

Then start your agent. With the hook or the pi extension installed, it reads the
new project and asks whether to continue. In another harness, say "Continue the
specflo project." The specflo-brainstorm skill takes over and hands off through
spec and plan to task-by-task execution. [Levels](#levels) describes quick,
fast, harden and research, and the [command reference](#command-reference)
lists every step.

## Using specflo with your agent

The installed skills make your agent *able* to drive specflo; a note in an
agent memory file makes it *routine*. Put it in one of three places:

- **Global** - `~/.claude/CLAUDE.md`, or your harness's user memory file. It
  applies to every repo you work in; the note only has the agent run specflo in
  a repo that has a `.specflo/` directory.
- **Local** - `CLAUDE.local.md` in the repo root, added to `.gitignore`. It
  applies to this repo and only to you, so a team repo's files stay unchanged.
  This file is a Claude Code feature. In another harness, a repo that does not
  track `AGENTS.md` can hold your own copy listed in `.git/info/exclude`.
- **Team** - the repo's `CLAUDE.md` or `AGENTS.md`, committed, for everyone who
  works in the repo.

`specflo guide` prints the note and tells the agent to ask you which place to
use. Add this near the top of the file:

```markdown
## Development workflow

The user develops features with specflo. In a repo that has a `.specflo/`
directory, run `specflo guide` at the start of a session to orient yourself;
`specflo status` shows the active project and phase. Features move through
brainstorm -> spec -> plan -> execute using the specflo skills, recording
decisions, requirements, and tasks through the specflo CLI rather than editing
its artifacts by hand.
```

Prompts that map onto the workflow:

- "Start a new specflo project for X." - creates the project, opens the brainstorm.
- "continue" - after a fresh start or a context clear, resume from the checkpoint.
- "Where are we?" - the active project's phase and next step (`specflo status`).
- "Park this for now." / "Pick that back up." - shelve and resume a project.
- "Run it in auto mode." - explicit opt-in to an unattended run (see `specflo auto`).

`specflo guide` runs cold - before `init`, in any repo - and orients a fresh
agent in one shot: what specflo is, the pipeline, the full command surface, and
what to do next. When in doubt, tell your agent to run it.

## The pipeline

Four phases, each gated by a validated artifact:

1. **brainstorm** - capture the idea; resolve open questions into recorded decisions (`D-NN`).
2. **spec** - synthesize testable requirements (`REQ-NN`), each traced to the decisions behind it.
3. **plan** - decompose into dependency-ordered tasks (`T-NN`), each with an acceptance criterion, a verify step, and the requirements it implements; optionally grouped into milestones (`M-NN`).
4. **execute** - work tasks one at a time; a completion gate confirms every task is done *and* that the final whole-branch review was recorded with a passing verdict before the project completes.

`specflo advance` validates the current phase's artifact before moving on, so a
hole in the spec stops the line early instead of surfacing mid-execution.

The end-of-execute review is an artifact too. `specflo review start` mints a
numbered `review-N.md` and `specflo review prompt` prints the brief for its
reviewer. The reviewer records each finding with `specflo review finding add`
(severity `blocker`, `should-fix` or `nit`, ID `F-NN`), and `specflo review
done` closes the round with the verdict its findings give: any blocker or
should-fix item is `changes-requested`, nits alone or `- none` is
`ready-to-merge`. Nits never block; they go to one follow-up per round. Every
round after the first reviewed one is a delta round: it reads only the diff
since that round and checks each earlier blocker and should-fix item with
`specflo review finding check`. A level may take `review_max_rounds` rounds (2
by default); past that, the user chooses one more round (`specflo review start
--over-budget`) or a waive (`specflo review waive`). Completing the project
requires the latest round to have closed with a passing verdict, so a review that
happened in some cleared context is no longer something you have to remember.

A blocker or should-fix finding names where its defect is (`--at
<file>:<line>[-<line>]`), and one on a line changed after the first reviewed
round is marked a regression, which is counted but changes neither the verdict
nor the budget. Each such finding is an open item until a round checks it
closed. Fix it with a task that names it (`specflo task add --fixes F-NN`):
`review start` opens no round while an open item has no done fix task. A
round that checks an item open records as failed the fix tasks that were done
when the round opened, and the item needs a new one. The brief lists each item's fix tasks and tells the reviewer to check an item
closed only when its pin test fails on the source at the latest reviewed
round's sha and passes on HEAD, with the defect gone on every path to it. On
the user's say, `specflo review finding defer` files an item as a follow-up
and `specflo review finding reject` records why it is not a defect; either one
settles it, and a changes-requested round whose blocking items are all settled
passes the gate. Every brief lists what earlier rounds settled, nits included,
and tells the reviewer to raise one again only with new evidence.

For a deeper look, `specflo review start --harden` opens a harden round: a
fresh review of the whole branch, outside the round budget, which closes
`hardened` whatever it finds. Its blocker and should-fix findings become items
to fix like any other. The completion gate reads the latest gate round's
verdict, never a harden round's, and fails while an item raised after that
round is open. After two harden rounds in a row with no new find, the
next-step hint suggests stopping; that is your call.

Set the `test_command` config key to the command that runs your whole test
suite (`specflo config set test_command "uv run pytest"`), and specflo names it
where the whole suite is due. The next-step hints that call for the whole suite
(before the first review round, and after a round that asked for changes
passes) name it in backticks, in `status`, `checkpoint`, `guide` and `advance`.
`specflo review prompt` tells the reviewer to run it each round, in place of
running only the tests for the files in scope; a project hosted on a daemon
gets the same brief, since the client passes its checkout's command. The
`specflo-quick` and `specflo-execute` skills run the command
`specflo config get test_command` prints at each step that runs the whole
suite. Unset, the hints ask for the whole suite without naming a command, and
the brief asks for only the tests for the files in scope.

Artifacts are plain markdown under `docs/projects/<slug>/` (configurable):

```text
$ specflo status
Project: Payment retries (payment-retries)
Dir:     docs/projects/payment-retries
Phase:   brainstorm
Execution: linear
Next:    Brainstorm and research; capture decisions, then write the spec.
Resume:  specflo checkpoint
```

Clearing context is free at any point: `checkpoint.md` is rewritten after every
state change, and `specflo checkpoint` prints the resume prompt that puts a
fresh session back to work.

## Levels

Not every change needs the whole pipeline. A project has one of five levels,
chosen with `specflo new <name> --level quick|fast|full|harden|research`. Three
of them build, with growing ceremony:

- **quick** - one goal and one check. The project has one phase, `execute`, and
  one document, `brief.md`, with the sections Goal, Done when, Proof and
  Deferred. `specflo validate brief` asks for a goal, exactly one check and
  proof, and `specflo advance` completes the project once it passes. There is no
  review round: the check and its proof are the gate.
- **fast** - 3 to 7 tasks. The usual brainstorm, spec and plan, written short by
  the agent: it makes at most 3 decisions itself, keeps going from brainstorm
  to plan without stopping, and stops once, when the plan validates, for the
  user's approval before execute. More than 3 active decisions or 7 active
  tasks only warns: the agent adds no more work and defers new work, and the
  project stays at fast. Execute finishes as at full level, after a passing review round.
- **full** - the default: every phase, with an approval at each.

The other two stand beside the pipeline: **harden** makes code that already
exists sound, and **research** builds knowledge over sessions, not code. Each
has its own subsection below.

`specflo doc show brief` prints one page at either light level: the brief file
at quick, and at fast the goal, decisions, checks and tasks drawn from the three
documents. Nothing is stored for the fast view.

A project moves up with `specflo level fast|full`, never down, and goes back to
the brainstorm with every document and every done task kept. From quick, the
brief seeds the three documents: the goal and the deferred items go into the
brainstorm, the check becomes `REQ-01`, and `T-01` implements it (done when the
brief has proof). From fast, the command lists the decisions to review with the
user, since the agent made them without an interview. From research, the move
scaffolds the brainstorm the project had none of and keeps the research
document and ledgers where they are, to ground that brainstorm in; research
moves up to fast or full only, never to quick, and no project moves to
research, which only `specflo new` starts a project at.

A cap never stops a `specflo auto` run and never moves the project up. A quick
brief with two checks is told to keep one and defer the rest. A fast project
past its caps only warns, since no command removes a decision or task. Moving
up is your choice, with `specflo level`.

### The harden level

`specflo new <name> --level harden` starts a project that makes code that
already exists sound, in the checkout or with `--remote`. It stands apart from
the three levels above: it never moves to another level, and `specflo level`
and `specflo auto` refuse it, since hardening stops on the user's say. The
project starts at `execute` with a `brief.md` of three sections - Scope (the
paths to harden, or the whole repo), Focus and Stop when - and an empty plan.

The work is a loop of harden rounds. Each one, opened with `specflo review
start --harden`, reviews all of the brief's Scope, where a problem already
present is a finding however old it is. Each blocker and should-fix finding
it records gets a fix task (`specflo task add --fixes F-NN`, the only kind of
task the project takes), and the next harden round checks the fixes. There is no task
cap and no round budget. The project completes when the brief validates, a
harden round has closed `hardened`, no round is open, every fix task is done
and no item is left open. The `specflo-harden` skill drives it.

### The research level

`specflo new <name> --level research` starts a research project: a
long-running, multi-session study of an idea, a market or a product, made
before anything is built, or instead of it. It has one phase, `research`, and
nothing is built from it: no brainstorm, spec, plan or review round. The agent
proposes it, like any level, from the request, and the signs that call for it
are these four: no build is intended yet, there are documents to bring in, a
domain expert rather than a developer sits at the keyboard, and much of the
ground is unknown. The `specflo-research-project` skill drives it;
[Research projects](#research-projects) describes the sessions and lists the
commands.

The project keeps a `research.md` with the sections Destination, Usual user,
Decisions and Log, and four ledgers under `research/`: sources (`S-NN`),
questions (`Q-NN`), findings (`RF-NN`) and glossary terms. Every file stays
inside the project folder, and only the CLI writes them. A research project is
local only: `--remote` and `specflo promote` refuse it, and `specflo auto`
refuses it as attended work, since a research session is a conversation.
`specflo decision add` records a decision in `research.md`; `specflo doc show
research` prints the document; `specflo status`, `specflo list` and the project
index show the level.

There are three ways on. `specflo level fast|full` turns the project into a
brainstorm at that level, with the research kept beside it; quick is refused.
`specflo research spinoff` starts a separate build project seeded with named
entries while the research goes on. And `specflo advance` completes the
project once a destination with its reason, the usual user and one closed
session are recorded; `specflo validate research` names whatever is still
missing.

### The ladder run

The ladder shows what each level adds on one task: quick first, then fast
built on quick, then full built on fast, each on its own branch, with one row
of numbers per level so you can see whether the extra process was worth it.
It is a tool for evaluation, not for day-to-day work. Use it to learn which
level a kind of task needs, or how much process a model needs.

`specflo auto --ladder` on a quick project runs all three levels in one
unattended run, each on its own local branch:

1. The quick level runs on `specflo/<slug>/quick`, cut from where you started.
2. When it completes, the next pass cuts `specflo/<slug>/fast` from there and
   moves the project up; the fast level builds on the quick result.
3. Then `specflo/<slug>/full` the same way. With no user to interview, the
   agent reviews each fast decision itself, takes up the deferred list, and
   must close a review round of its own, which the climb opens.
4. After full completes, one more `specflo auto` pass writes the last row and
   stops, naming the three branches.

At every level `specflo advance` ends only that level; the texts it and
`status` print say to run `specflo auto` again, and `status --json` keeps
reporting the run as under way until the closing pass.

Inside a ladder, caps work as in any auto run, and the next level picks up the
deferred work. A fast branch can be larger than fast allows. While a
ladder runs, `specflo level` is refused: the ladder moves the level up itself.
A level's row counts commits up to its branch tip. `ladder.md` in the project
directory records the base branch and commit and one row per level: commits, files and
lines changed against the level's start, tasks, test result, review verdict,
deferred items and time. A level that reaches its review budget is waived by the
run itself, and its review cell reads `waived (budget)` with the items still
open, which the next level's first round must check. The test result comes from the optional `test_command`
config key (`specflo config set test_command "uv run pytest -q"`), run on each
level's branch; unset, the row says `not run`. Review the three branches and
merge the one you like best. The ladder needs a clean tree (specflo's own
documents excepted), never pushes, and never deletes, renames or resets a
branch.

To compare models, set `test_command`, run the same task as a ladder on two
or three models of different sizes, and add one quick run on a large model as
a reference. If a small model at full comes close to the large model at quick,
the process makes up for model size on that task. Each level starts from the
one below it, so a good full branch does not show that full alone would beat
quick alone. `ladder.md` does not record the model or the harness, so note
them yourself.

## Research projects

A research project ([the research level](#the-research-level)) is worked in
sessions, each a conversation with the user, who is often a domain expert
rather than a developer. The `specflo research` commands keep the document,
the four ledgers, the log and the grounding check; the
`specflo-research-project` skill carries the conversation. The loop of a
session:

1. **Open with `specflo research recap`.** It prints where the research is
   headed and why, who the usual user is, what the last session did and left
   for the next one, the open questions by who answers them, the ledger
   counts, any spin-off that rests on a replaced entry, and the rule for an
   outgoing web search. The checkpoint of a research project lists
   `research.md` first and tells the next agent to start here.
2. **Record each source, finding, question, term and decision as it lands**,
   with the commands below, so a session that stops early loses nothing. On
   the first session the usual user and the destination are recorded too.
3. **Close with `specflo research session close`**, with a summary and the
   next steps, so the next recap starts the next session.

The rules the commands hold to:

- Entries are never deleted. A wrong or overtaken entry is replaced with
  `--supersedes`; the old one stays in its ledger, marked, and the recap names
  any spin-off that rested on it.
- A fact needs a citation. A citation is a source, `S-NN`, or a page of one,
  `S-NN:pN`; a page is accepted only on a PDF and only within its page count.
  An inference may stand on none.
- `research ingest` takes `.md` and `.txt`, and with the `docs` extra `.pdf`
  and `.docx`. The original is stored byte for byte under
  `research/sources/S-NN/` with its text beside it. The same bytes again are
  refused naming the source that holds them; so is a scanned PDF with no text
  layer, a file over `research_source_limit_mb` (default 50) and a conversion
  past `research_convert_timeout` (default 120 seconds); both limits are set
  with `specflo config set`.
- The first source of a project that pins no egress class prints one
  suggestion to run `specflo egress`. Whatever the class, the skill keeps
  names, private figures and source text out of outgoing web searches.
- `research lint` is a report, not a gate: no command depends on it passing.
- No research command needs or writes to a wiki; the research lives in the
  project folder.

The commands, all on the active project:

- `specflo research ingest <file>` - keep a `.md`, `.txt`, `.pdf` or `.docx` file as a document source (`S-NN`): the byte-identical original, its text, name, sha256 and date. PDF and DOCX need the `specflo[docs]` extra; a PDF records its page count.
- `specflo research source add --kind interview|web|repo --title <text> (--file <path> | --stdin) [--url <url>]` - record an interview, web or repo source (`S-NN`) with its text stored as given; web and repo need `--url`.
- `specflo research user set --text <text> | --file <path>` - record who usually sits at the keyboard, in their own words; a later call replaces it and the log keeps the old text.
- `specflo research destination set build|inform|learn|unknown --reason <text>` - record where the research is headed and why; the log keeps the old value, the new one and the reason.
- `specflo research question add --text <text> --by user|developer [--option <text>]... [--default <text>] [--why <text>] [--supersedes Q-NN]` - record an open question (`Q-NN`): who answers it, its options, default and why. A developer question needs a default, so the work goes on when no answer comes.
- `specflo research question resolve <Q-NN> --resolution <text> [--by <name>]` - resolve an open question: record the resolution and who gave it (the usual user by default).
- `specflo research ask-dev` - print the open developer questions as email text (a subject line and a plain body) for the user to send; it writes nothing and sends nothing.
- `specflo research finding add --text <text> --kind fact|inference --status supported|contradicted|unsure [--cites S-NN[:pN]]... [--supersedes RF-NN]` - record a finding (`RF-NN`) with checked citations: a fact needs one, and a page only on a PDF and within its page count.
- `specflo research term set --term <term> --definition <text> [--avoid <word>]...` - set a glossary term in the user's own words, with the words to avoid; setting it again replaces the definition in place and the log keeps the old one.
- `specflo research list sources|questions|findings|terms [--open] [--json]` - list a ledger's entries with their IDs and fields; `--open` keeps the open questions, `--json` gives every entry with its status.
- `specflo research lint` - check each finding's numbers, dates and quoted strings against the text of the sources it cites (within the cited page of a PDF); one line per missing figure, exit 1 with any. It writes nothing and gates nothing.
- `specflo research session close --summary <text> --next <text> [--present <who>]` - end a session: one dated log entry with the summary, the next steps and who was present (the usual user by default).
- `specflo research recap [--json]` - print the session recap described above; writes nothing.
- `specflo research spinoff <name> --level quick|fast|full --use <ID>[,<ID>]...` - spin a seeded build project off the research: create it as `new` would, with a `handoff.md` holding each named source, question or finding in full. The research project stays as it is and its log names the spin-off. When the research later supersedes an entry the handoff rests on, the recap lists the spin-off and `specflo status` in the spin-off prints one line saying so.

Decisions go through the usual `specflo decision add`, which writes them into
`research.md`; `specflo doc show research` prints the document, and
`specflo validate research` and `specflo advance` complete the project (see
[the research level](#the-research-level)).

## Command reference

### Global option: run in another directory

- `specflo -C DIR <command>` / `specflo --directory DIR <command>` - run the command as if specflo had been started in `DIR`. Every command honors it, including `init`, `hook reseed`, `auto`, and `extension install --scope project`; root discovery is the usual walk up from `DIR` to the nearest `.specflo/config.yaml`. `DIR` must exist and be a directory, or the command exits 2 before anything runs. The option goes before the subcommand.
- `SPECFLO_DIRECTORY=DIR` - the environment variable has the same effect. Precedence is the flag, then the env var, then the current directory; a set-but-empty variable counts as unset.
- Relative path arguments given to the subcommand (`review done --file report.md`, `init --projects-dir custom`) resolve against `DIR`, not the caller's cwd - the same contract as `git -C`.
- The change lasts only for the command: the process cwd is restored when the command finishes.
- `SPECFLO_DIRECTORY` reaches everything that inherits the environment: the Claude Code SessionStart hook, the pi extension (its specflo calls and its statusline segment), and any subagent an orchestrating agent spawns. This is the intended effect for an agent driving a project in another tree. To make the redirect visible, `specflo status` prints a `Root: <path> (via -C|SPECFLO_DIRECTORY)` line (and `root` / `directory_source` keys in `--json`) when the directory was overridden, and the reseed payload from `specflo hook reseed` leads with one line naming the root when the env var is set. Without an override, all outputs are unchanged.

### Global option: act on another project

- `specflo -p NAME <command>` / `specflo --project NAME <command>` - run the command on project `NAME` instead of the active project, without switching to it. `specflo -C ../other -p foo followup add "..." --do "..."` records a follow-up in `foo` in another checkout and leaves that checkout's active project as it was. The option goes before the subcommand, and `NAME` is resolved in the checkout `-C` selects.
- `SPECFLO_PROJECT=NAME` - the environment variable has the same effect. Precedence is the flag, then the env var, then the stored active project; a set-but-empty variable counts as unset.
- `NAME` must be a project in the checkout or one hosted on a daemon. Otherwise every project verb exits 1 before it runs and creates nothing. Commands that act on no project (`init`, `new`, `list`, `index`, `guide`, `doctor`, `config`, `skills`, `extension`, `serve`, `bench`, `remote`, `product`, `workitem`, `console`, `agent`) ignore the option. `lease request` asks for the member on behalf of the `-p` project, so the daemon applies that project's egress pin.
- The override is never written to `.specflo/config.yaml`. Only the verbs whose job is to move the pointer move it: `-p foo switch` makes `foo` active, `-p foo resume` resumes `foo` and makes it active, and `-p foo leave` clears the pointer only when `foo` is the active project (otherwise it changes nothing). `-p foo shelve` shelves `foo` and leaves the pointer alone.
- A command that also takes a project name (`summary`, `shelve`, `resume`, `switch`) runs when the name and `-p` name the same project, and exits 1 before any change when they differ.
- `specflo status` ends the Project line with `(via -p)` or `(via SPECFLO_PROJECT)`, and `--json` adds a `project_source` key (`flag` or `env`), when the project was overridden.
- `SPECFLO_PROJECT` reaches everything that inherits the environment, including the pool members and agents specflo starts. The `-p` flag does not.

### Setup and orientation

- `specflo --version` - print the installed version and exit.
- `specflo guide [daemon] [--json]` - orientation in one shot: what specflo is, the pipeline, the command surface, and what to do next here. Runs **cold** (works before `specflo init`), so a fresh agent can get up to speed in any repo. The daemon and team commands (serve, remote, promote, product, workitem, gate, lease, console) are listed by `specflo guide daemon`; `--json` carries every command.
- `specflo init` - scaffold `.specflo/config.yaml` + the projects dir (default `docs/projects/`).

### Configuration

- `specflo config get <key>` - print one setting's resolved value, bare on stdout, so `$(specflo config get autonomy)` is the value itself. An unset key prints its shipped default.
- `specflo config list [--json]` - every setting with its resolved value, in registry order. A line ends with `(default)` when the file is silent about that key, or `(invalid, using default)` when the file's value is not one the key accepts. Keys specflo does not recognize are listed separately and left alone. `--json` reports each key's `value` and a `source` of `set`, `default`, or `invalid`.
- `specflo config set <key> <value> [--force]` - set one setting. The value is coerced to the key's type and validated **before** the write, so a rejected value leaves the file untouched and the error names what the key accepts. `active_project` is refused (use `specflo switch`), and `projects_dir` needs `--force` while projects live under the current path - changing it moves nothing, it only changes where specflo looks.
- `specflo config unset <key> [--force]` - drop one setting; it returns to the commented-out default line under its description, and reads as its shipped default again.

See **[The config file](#the-config-file)** for the file itself.

### Projects

- `specflo new <name> [--execution linear|fan-out] [--level quick|fast|full|harden|research]` - create a project and make it active. `--execution` records the execution mode in `project.md` (default `linear`; see "Execution modes and fan-out" below). `--level` records how much ceremony the project gets (default `full`; see [Levels](#levels)); a quick project starts at `execute` with a `brief.md`, a harden project at `execute` with a `brief.md` and an empty `plan.md`, and a research project at `research`, its one phase, with a `research.md` and four empty ledgers under `research/`. Quick, fast and research are refused with `--remote`.
- `specflo level fast|full` - move the active project up a level, back to the brainstorm phase, keeping every document. Refuses the same or a lower level, research as a target, and any move of a harden project. From research the brainstorm is scaffolded and the research document and ledgers stay as they are; quick is refused there, naming fast and full.
- `specflo execution linear|fan-out [--json]` - switch the active project's execution mode, in either direction, at any phase. Reports `unchanged` when the mode already matches; `--json` emits `{execution, changed}`.
- `specflo list [--json]` - list all projects, marking the active one, each with its phase and level as `(phase, level)`, so a quick fix, a research project and a full build read apart; `--json` carries both fields. The project index (`specflo index`) has a Level column too.
- `specflo switch <name>` - make another project active (by slug or name).
- `specflo status [--json]` - show the active project, its phase, its level and what's next. In a project spun off a research project, one more line says when the research has since replaced an entry its `handoff.md` rests on.
- `specflo shelve [<name>] [--reason ...]` - set a project aside: status `shelved`, phase untouched.
- `specflo resume [<name>]` - pick a shelved project back up at the phase where it was paused.
- `specflo leave [--json]` - clear the active-project pointer without changing any project. Nothing is written to the project; re-enter it later with `specflo switch <name>` (or `specflo resume <name>` if shelved). Idempotent: with no active project it prints `No active project.` and exits 0.

### Phase artifacts

- `specflo brainstorm start [--json]` - create (or locate) the active project's `brainstorm.md`.
- `specflo decision add --text ... [--rationale ...] [--supersedes D-NN]` - append a decision (`D-NN`) to the brainstorm, or to `research.md` in a research project; after a move up the one numbering sequence runs on across both documents. `--text` and `--rationale` are one line each: a value carrying a line break is refused. Text an active decision already holds is refused, ignoring case and extra whitespace, unless `--supersedes` names that decision; the message tells a looping model to stop and check what is recorded.
- `specflo spec start [--json]` - create (or locate) the active project's `spec.md`.
- `specflo requirement add --text ... --acceptance ... [--from D-NN] [--supersedes REQ-NN]` - append a requirement (`REQ-NN`) to the spec. `--text` and `--acceptance` are one line each: a value carrying a line break is refused. Text an active requirement already holds is refused in the same way as for `decision add`.
- `specflo plan start [--json]` - create (or locate) the active project's `plan.md`.
- `specflo task add --text ... --acceptance ... --verify ... --from REQ-NN [--from REQ-NN ...] [--fixes F-NN ...] [--depends-on T-NN ...] [--files ...] [--needs <pool> ...] [--supersedes T-NN]` - append a task (`T-NN`) to the plan. `--from` (repeatable, required unless `--fixes` is given) links to the requirement(s) the task implements; `--fixes` (repeatable) names the review findings the task fixes, on a `- Fixes:` line that `task show` and `task list` print. `--fixes` accepts only an open item: a blocker or should-fix finding of a closed round that is not checked closed, deferred or rejected. At harden level every task needs `--fixes`. `--depends-on` (repeatable) declares execution ordering; `--acceptance` is a behavioural pass/fail criterion; `--verify` is the command or step to confirm it. `--files` lists the files the task edits (comma-separated; one trailing parenthetical note per entry is allowed and stripped, as in `~/x/y (venv, outside repo)`); `--needs` (repeatable) names a resource pool the task needs - a non-empty token without commas or whitespace, such as `gpu:3090`. `--text`, `--acceptance`, `--verify`, `--files` and `--scope` are one line each: a value carrying a line break is refused. Text an active task already holds is refused in the same way as for `decision add`.
- `specflo pool add <name> --size N [--json]` / `pool list [--json]` - declare a pool of `N` slots (`N >= 1`) in the CLI-owned `## Pools` section of `plan.md`, updating the size in place on a repeated add; `pool list` shows every declared pool plus every pool an active task needs. A pool nobody declared has one slot. `user` is a reserved pool name meaning the task runs in the main session with the user; it needs no declaration.
- `specflo plan graph [--json]` - render the plan's execution graph from its real data: waves by longest dependency path (wave 0 has no dependencies), one line per active task (id, title, progress, files, needs) and a mermaid `graph LR` block with one node per task, a subgraph per milestone and one edge per `Depends on` entry. `--json` emits `{waves, tasks, edges}`. Read-only: `plan.md` is byte-identical afterwards.
- `specflo milestone add --text ... --exit ... [--exit ...]` - append a milestone (`M-NN`) with its Exit checklist to the plan; `milestone list` and `milestone show` report rollup and the current milestone. `--text` and each `--exit` item are one line: a value carrying a line break is refused.
- `specflo doc show brainstorm|spec|plan|brief|research|checkpoint|project|followup|review-<N>` - print one artifact of the active project verbatim; `review-<N>` is a review round by its number. Agents read artifacts through this verb rather than opening files, so the same command serves a project in the checkout and one held by a daemon. An unknown name is refused with the valid names listed. `brief` is the quick or harden project's brief, or at fast level a one-page view of the three documents; it is refused at full level. `research` is the research project's document.
- `specflo section set brainstorm|spec|plan|brief <section> --file <path>|--stdin` - replace one prose section's body (named with or without its `##`), keeping the header, every other section and every managed entry byte-identical and bumping `updated`. The managed sections (Decisions, Requirements, Tasks, Milestones, Pools) are refused with the verb that owns them.
- `specflo validate brainstorm|spec|plan|brief|research [--json]` - lint the phase's artifact and report readiness. `brief` checks a quick project's goal, its single check and its proof, or a harden project's Scope, Focus and Stop when. `research` names what still blocks a research project's completion (a destination with its reason, the usual user, one closed session), each with the command that records it. The plan lint checks bidirectional REQ<->task coverage (a fix task needs no requirement, and a harden plan may be empty), that every task has acceptance + verification, and that dependencies resolve and are acyclic.

### Working the plan

- `specflo task start <T-NN> [--force] [--worktree]` / `task done <T-NN> [--note ...] [--commit <sha>] [--closes <FU-NN> ...]` - mark a task `in_progress` / `done`. `task start` refuses a task whose dependencies are not all done, naming each one; `--force` starts it anyway. `task start --worktree` gives the task its own git worktree at `.specflo/worktrees/<project>/<T-NN>` on a new branch `specflo/<project>/<T-NN>` cut from HEAD, records the worktree and its base branch and sha on the task, and prints the path; it refuses a task already in progress. Each path listed in `.specflo/worktree-include` (one per line, relative to the checkout root, for files git does not carry such as a `.env`) is copied into the new worktree, and a listed path that does not exist is named. Worktree starts run one at a time under a lock. `task done --commit` records the task's commit as a full sha in a `- Commit:` field; a value that names no commit in the checkout is refused. `task done --note` records the note in the same write as the state change. `task done --closes FU-NN`, repeatable, closes each follow-up the task settles with `--by <project>/<T-NN>` and the task's note, or else its title (`Task done.` for a blank title), as the close note, and `--json` lists them in `closed_followups`. Every named follow-up is checked open, and every followup document readable, before the task changes. `--closes` is refused for a project hosted on a daemon.
- `specflo task block <T-NN> [--reason ...]` / `task reopen <T-NN> [--note ...]` - mark a task `blocked` (optionally recording why) / return it to `pending` (optionally with a note). `task start` and `task block` take no `--note`. Reopening a worktree task first saves what its worktree holds against the base sha as a patch under `.specflo/worktrees/patches/<project>/`, prints its path (`patch` in `--json`), then removes the worktree and its branch.
- `specflo task land <T-NN> [--message <text>] [--json]` - turn what a worktree task holds, committed or not, into exactly one commit on its base branch: the change is replayed onto the branch's tip, the checkout is fast-forwarded, the commit is recorded in the task's `Commit:` field, and the worktree and its branch are removed. The message is `--message`, else the task title. The task stays `in_progress`: run its verify step on the base branch, then `task done`. Files the commit changed outside the task's `Files` are printed and recorded in a note on the task. It refuses, changing nothing: a task that is not in progress or runs in the shared tree, a change under the projects directory, a checkout on another branch, an uncommitted change in the checkout to a file the task changes (other uncommitted files, such as `plan.md`, do not block it), and a conflict with the base branch, which it names by file with no rebase left in progress. `task start --worktree` and `task land` are refused for a project hosted on a daemon.
- `specflo task edit <T-NN> [--title ...] [--acceptance ...] [--verify ...] [--scope ...] [--files ...] [--needs ...] [--implements REQ-NN[,REQ-MM]] [--add-depends-on T-NN ...] [--drop-depends-on T-NN ...] [--force] [--json]` - correct an active task's fields in place, rather than hand-editing `plan.md`. At least one edit flag is required; a field already holding the given value is reported as unchanged rather than rewritten. Every value is one line: a value carrying a line break is refused, and `--title`, `--acceptance`, `--verify` and `--implements` refuse an empty value (an empty `--scope`, `--files` or `--needs` clears that optional field). `--add-depends-on` and `--drop-depends-on` are repeatable and are refused before any write: an added edge when the named task does not exist, is superseded, is the task itself, or would close a dependency cycle; a dropped edge when the task does not hold it (so an edge onto a task that no longer exists can still be dropped). A **done** task refuses the edit unless `--force`, which rewrites the field and appends one `[Edit]` note per changed field carrying the value it overwrote; a **superseded** entry is frozen with or without `--force`. `--json` emits `{id, changed}`.
- `specflo task note <T-NN> --text ... [--label Note|Design|Resolution|Descoped] [--json]` - append one dated note line to a task entry: `- Note: <YYYY-MM-DD> [<Label>] <text>`. Notes accumulate at the bottom of the entry in the order written and work on a task in any progress state, including a done or superseded one. The label set is closed and `Edit` is reserved for `task edit --force`; the text is written as a single line (runs of whitespace and newlines collapse to single spaces) and empty text is refused. Notes surface in `specflo task show` and nowhere else - `task list`, `status` and `checkpoint` are unaffected - and a hand-written note that does not parse is a non-blocking plan warning, never a validation failure. `--json` emits `{id, note}`.
- `specflo task list [--json]` - all tasks with their progress state and the deps-aware next-actionable marker. `--json` is the orchestrator's frontier: each task also carries `files`, `needs` and `ready` (true exactly when the task is pending and next-actionable; an in-progress task is never ready, even when the next-actionable marker falls back to it because every pending task is held back), and the payload carries `pools`, a map from pool name to `{size, holders}` where holders are the in-progress tasks needing that pool.
- `specflo task show [<T-NN>] [--json]` - a task's brief: acceptance criterion, cited requirements, and constraints, plus the execution mode and, when set, `Files:` and `Needs:` lines (a task needing `user` is marked as not delegated). For each task it depends on it prints that task's title, recorded commit and notes (`dependencies` in `--json`). A task running in a worktree gets the worktree path and the rule to edit and test only there and never commit. On a fan-out project the brief also carries the subagent rules: the read-only git a subagent may run (`git diff`, `git log`, `git show`, `git status`), no `git stash`, other git writes or specflo, the test policy, and leave to update a test that pins a fixed list. Defaults to the next actionable task.
- `specflo review start [--full] [--over-budget] [--harden] [--json]` - mint the next numbered review round (`review-N.md`) in the project directory and print its locator and scope. The round records `HEAD` of this checkout (inside a git repo; for a project a daemon holds too, since the code lives here), the project's level and its base. A round already open that nobody has written into takes `HEAD` again when `review start` hands it back, so a round opened early (a ladder opens one when it climbs) starts its range at the commit the reviewer read. Round 1 reviews the whole branch. After a reviewed round (not a waived one), the next round is a delta round: it prints the range `<sha>..HEAD` from the latest reviewed round's sha and the earlier blocker and should-fix items it must check. `--full` reviews the whole branch again and keeps the items. Numbering only ever goes up, so a deleted round leaves a permanent gap rather than a reused identity. With a round already open, prints that round and mints nothing - reusing it is how an abandoned review is resumed. When the latest round is `changes-requested` and the level has used its `review_max_rounds` rounds, it opens nothing and names the two ways on; `--over-budget` opens one more round, and each further round needs the flag again. Rounds of an earlier level do not count. No round opens, with any flag, while an open blocker or should-fix item has no done fix task (`task add --fixes`); the refusal names each item. `--harden` opens a harden round (`Kind: harden`): a fresh review of the whole branch, or in a harden project of the brief's Scope, that the budget neither refuses nor counts; it still checks each open item. `--json` adds `kind` (`gate` or `harden`), `scope` (`whole-branch` or `delta`), `range` and `items`.
- `specflo review prompt` - print the reviewer brief for the open round: its scope and the items to check, each with the tasks that fix it and their verify steps, and when to check an item closed (its pin test fails on the source at the latest reviewed round's sha and passes on HEAD, and the defect is gone on every path that reaches it); under `## Already settled`, the nits, deferred and rejected findings and recorded follow-ups of earlier rounds, to raise again only with new evidence; what `blocker`, `should-fix` and `nit` mean (wording in agent-facing text is a nit unless it tells the agent to do the wrong thing), that a problem the branch did not introduce or one outside the delta range goes to `specflo followup add`, how to record, that the CLI sets the verdict, and to run only the tests in scope, or the `test_command` each round when that key is set. A harden round's brief asks for evidence in the text of each blocker and should-fix, says wording in agent-facing text and docs is not a finding unless it tells an agent or user to do the wrong thing, keeps nits in the round, and runs only the tests that reproduce a finding, with the whole suite once when hardening stops. Refused with no round open.
- `specflo review finding add --severity blocker|should-fix|nit --text ... [--at <file>:<line>[-<line>]] [--json]` - append `- F-NN (severity) [file:line] text` to the open round's Findings section and print the new `F-NN`. `--at` names where the defect is, as the file is at the round's sha; a blocker or should-fix needs it, and a nit may leave it out. The CLI checks that the file has those lines at the round's sha in this checkout, and marks the finding `(severity, regression)` when git blame shows a line of it changed after the project's first reviewed round; for a hosted project git runs here, never on the daemon. When git cannot tell, a note on stderr says so and the finding is recorded. IDs run across every round of the project, and two adds at once never share one. A reviewer with no shell may write the same lines by hand. An unknown severity, an empty text, a line break or a location the round's sha does not have is refused and nothing is written.
- `specflo review scope "<line>" [--json]` - write one line under the open round's Scope reviewed section, replacing any line there. Refused with no round open, or for an empty or multi-line value.
- `specflo review clean [--json]` - record the open round as one with no findings: `- none` as the only line under Findings. Refused with no round open, or when the round records a finding already.
- `specflo review finding check <F-NN> closed|open [--json]` - write `- F-NN closed` or `- F-NN open` under the open round's Earlier findings section. Only a blocker or should-fix finding of an earlier round that no reviewed round has checked closed is accepted; a second check of an item replaces the first.
- `specflo review finding defer <F-NN> --do <what> [--json]` - settle an open blocker or should-fix item by filing a follow-up for it, and write `- F-NN deferred FU-NN` under `## Settled` in the round that recorded it. Refused for a project hosted on a daemon.
- `specflo review finding reject <F-NN> --reason <why> [--json]` - settle an open blocker or should-fix item as not a defect, and write `- F-NN rejected: <why>` under `## Settled` in the round that recorded it. A settled item leaves the ledger: no round checks it and no task fixes it. Use defer and reject only on the user's say.
- `specflo review done [--verdict ...] [--reason ...] [--file <path>] [--json]` - close the open round with the verdict its findings give, and print the count per severity and, when there are any, of regressions (`regressions` in `--json`). Any blocker or should-fix finding, or an earlier item checked open, is `changes-requested`; only nits, or `- none` as the whole Findings section, is `ready-to-merge`. A harden round closes `hardened` whatever it found, keeps its nits in the round, and prints its new finds (`new_finds` in `--json`). A finding line written by hand is marked a regression by the same rule as `review finding add`. It refuses, leaving the round open, a line under Findings not in the `- F-NN (severity) [file:line] text` form (a nit may leave out the location), a blocker or should-fix with no location, an `F-NN` used elsewhere in the project, a section with neither findings nor `- none`, `- none` beside findings, and an earlier item with no check line; the message names the line or ID and the ways on. `--verdict` is accepted only when it equals the derived verdict, except `--verdict waived --reason ...`, which closes the round without reading its findings. A round with nits adds one follow-up, `Nits from review round N`, naming their IDs; follow-ups work only for projects in the checkout, so a round a daemon holds keeps its nits in the round file. The date is stamped at close; the sha stays the one stamped at `review start`. `--file` ingests a reviewer's report as the round's body, refusing once that body has been written into. specflo never derives staleness from the stamp: commits landing after a closed round change nothing.
- `specflo review waive --reason ... [--json]` - close the open round `waived` with the reason, or, with no round open, record a round and close it `waived` in one step. Works at any time, past the round budget, and while an open item has no done fix task. An empty reason is refused.
- `specflo validate execute [--json]` - completion gate: confirms every task is done, then that no round is open and the latest gate round closed `ready-to-merge` or `waived`, or `changes-requested` with every blocking item deferred or rejected, and that no item raised after that round is still open. A harden round's verdict is never the gate's. The gate keys on the verdict and the open items, never on the round's nits, so a passing round may still list some. A harden project needs a valid brief, a harden round closed `hardened`, no open round and no open item instead.
- `specflo advance [--json]` - validate the current phase's artifact, then move the active project to the next phase (`brainstorm -> spec -> plan -> execute`). On a research project, whose one phase is terminal, it completes the project once `validate research` passes.
- `specflo reopen [<phase>]` - the inverse of `advance`: move the phase pointer backward (bare `reopen` goes one phase back, `reopen <phase>` jumps to a named earlier phase). A pure pointer move; no artifact is rewritten.
- `specflo checkpoint [--json]` - print the active project's **resume prompt** (which phase, what to read, what to do next) and refresh `checkpoint.md`. The file is also rewritten automatically after every state-mutating command, so a freshly-cleared agent can jump back in with one command.

### Follow-ups

The work a project leaves for a later one. Each project keeps its entries in its own `followup.md`, created on the first add. The `FU-NN` numbers run across every project in the checkout, hand-written followup documents included, so an ID needs no project prefix. Only the followup verbs write the document: `section set` refuses it, and `doc show followup` prints it.

- `specflo followup add <title> --do <text> [--from <text>] [--json]` - add an open entry to the active project: a `### FU-NN - <title>` heading, a `- Do:` line, a `- From:` line when given, and `- Status: open`. Each value is one line, and an empty title or Do is refused. Two adds at the same time never get the same ID. Refused for a project hosted on a daemon.
- `specflo followup close <FU-NN> --note <text> [--by <ref>]` - close an open entry in any project of the checkout: its Status becomes `closed` and a `- Closed: <date>: <note>` line is added. `--by` names what did the work on a `- Closed by:` line below it: `<project>/<T-NN>` for a task in that project's plan, `<project>` for a project of the checkout, or a git commit, stored as its short SHA. A word that is both a project and a commit names the project. An unknown ID, an entry already closed, a hand-written entry, a missing or empty note, and a `--by` ref that names no project, task or commit are refused with nothing written.
- `specflo followup list [--all] [--json]` - the open entries of every project in ID order, with project, ID, title and Do line. `--all` adds the closed ones. Hand-written entries have no Status line and are not listed.
- `specflo followup show <FU-NN> [--json]` - one entry, open or closed, from any project of the checkout: project, ID, title and its Do, From, Status, Closed and Closed by lines (`closed_by` in `--json`, null when the close named no work). When two projects hold the ID, the open one is shown. An unknown ID or a hand-written entry is refused. `specflo guide` tells an agent to run it when the user names a follow-up to do.

`specflo advance` lists the open follow-ups of the project it completes (`followups` in `--json`), and `specflo new` prints how many open follow-ups the checkout holds.

### Research commands

The `specflo research` commands (ingest, source add, user set, destination set, question add, question resolve, ask-dev, finding add, term set, list, lint, session close, recap, spinoff) are listed in [Research projects](#research-projects).

### Session-start and unattended runs

- `specflo hook reseed [--format text|claude] [--continue]` - emit the **clear-and-continue** payload for the active project: a confirmation-gate directive (*do not start work; present the checkpoint and ask whether to continue*) followed by the verbatim checkpoint. Prints nothing for no active project, or when the active project is complete or shelved (nothing to resume, so the session starts silent). **Always exits 0, reads no stdin, makes no network calls** - safe to wire into a session-start hook unconditionally.
  - `--format claude` wraps the payload as Claude Code `SessionStart` JSON: the payload as `additionalContext` (re-grounds the agent) plus a user-visible `systemMessage` that tells you **what to type** to kick it off (Claude can't make the agent take a turn on its own). The default `--format text` stays portable plain text for any harness.
  - `--continue` swaps the confirmation gate for a direct *carry out the next step now* directive, and inlines the current task's brief - for a caller that cleared context on purpose and has already answered "keep going".
- `specflo hook install` - idempotently merge the `SessionStart` wiring into Claude Code's `.claude/settings.json`, preserving all existing content; a previously-installed (older) reseed entry is rewired in place rather than duplicated. The wiring calls `specflo hook reseed --format claude` on the `startup`, `clear`, and `resume` sources (`compact` excluded - its digest is retained).
- `specflo hook print` - print that same wiring as a JSON fragment on stdout (pipeable), either to merge into Claude Code's settings yourself or as a starting point to adapt for another harness (opencode, OpenAI Codex, ...); a stderr note marks it as a fragment and points at `specflo hook install` as the safe merge. pi needs no wiring - the bundled pi extension reseeds on its own (see **[The pi extension](#the-pi-extension)**). (`hook print --install` remains as a deprecated alias of `hook install`.)
- `specflo auto [--autonomy safe|autonomous|yolo] [--max-passes N] [--off|--on] [--ladder] [--json]` - emit the **auto-mode handoff payload**: an explicit, per-invocation opt-in that starts or continues an *unattended* run from the current phase toward project completion, emitting a bootstrap directive (autonomy policy + guardrail stop-conditions) instead of the ask-first pause. specflo only prints the payload - it drives no loop, spawns no nested agent, and never clears context; the clear-and-reseed trigger is the outer harness's job. Strictly additive - the default `hook reseed` / checkpoint behavior is unchanged. A harden or research project is refused, naming its level: both are attended work, with the user at the keyboard.
  - `--autonomy` sets how far it runs unattended: `safe` (the default) and `autonomous` stop and hand off on any irreversible or outbound step; `yolo` permits them. Overrides the `.specflo` config default.
  - `--max-passes` is a runaway backstop: each invocation counts as one pass in a durable per-project run-state file, and on reaching the cap (default `50`) the run escalates to the human instead of continuing. Overrides the config default.
  - `--off` sets the durable kill switch (the next pass halts); `--on` clears it.
  - When the latest review round is `changes-requested` and the level has used its `review_max_rounds` rounds, the pass stops with the reason `review-budget` and names `specflo review start --over-budget` and `specflo review waive --reason <why>`: the next round, or a waive, is the user's call. A ladder run waives the level instead and goes on.
  - `--ladder` starts a [ladder run](#the-ladder-run) on a quick project; later passes continue it without the flag.
  - `--json` reports the pass as an object - its `payload` text, a boolean `stop`, and the `reason` that stopped it (`kill-switch`, `pass-cap`, `stall`, `project-complete`, `ladder-blocked`, `review-budget`, or `unavailable`; `outgrew-level` is listed but no longer sent; `null` while the run continues) - so a machine caller reads loop control from the CLI instead of deciding it.

### Harness integration

- `specflo skills install|status|update|uninstall [--scope user|project] [--harness NAME[:SCOPE]]` - install specflo's bundled workflow skills into the agent harnesses on your machine, and keep them current. See **[Skills](#skills)**.
- `specflo doctor [--json]` - check the setup on this machine: the `specflo` command is on PATH, and each detected agent harness has every bundled skill, installed by `specflo skills install` or as a link to the same content. A missing, stale or locally modified skill and a broken or mismatched link each fail with the command that fixes it, and so does a machine where no harness has every skill. A harness with no specflo skills is listed and skipped. Runs cold (works before `specflo init`). Exits 1 when a check fails. `--json` emits `{ok, checks}`.
- `specflo extension install [--scope user|project]` - install the bundled pi extension into pi's extension directory: `~/.pi/agent/extensions/specflo` by default, `./.pi/extensions/specflo` with `--scope project`. A plain local copy with a version stamp - no npm, no network - and pi discovers the directory on its own, so no pi settings are read or written. Re-run to update. See **[The pi extension](#the-pi-extension)**.

### Daemon, pool, gate and product commands

The commands for hosted projects, the agent pool, gates, and products and work
items are in [DAEMON.md](https://github.com/TacoTakumi/specflo/blob/main/DAEMON.md#command-reference).

## Execution modes and fan-out

Every project records an execution mode, `linear` (the default) or `fan-out`.
Set it with `specflo new --execution ...` or change it at any time with
`specflo execution ...`. `status`, `checkpoint` and `task show` show it.

The easiest way to get subagents is to ask your agent: "start the execute
phase in fan-out mode". The agent runs `specflo execution fan-out` and the
execute skill makes it the orchestrator.

- **linear** - one agent works the plan one task at a time.
- **fan-out** - the main session is the orchestrator. It starts each ready
  task with `task start --worktree` and gives it to one subagent, which edits
  and tests only in that worktree. When the subagent returns, the orchestrator
  runs the task's verify step, lands the change with `task land` (one commit
  per task on the base branch) and runs the verify step again before
  `task done`. Subagents never run `specflo` or a `git` command that writes.

A task is ready when its dependencies are done, no in-progress shared-tree task
edits one of its `Files`, and every pool in its `Needs` has a free slot
(`pool add` sets the size, and an undeclared pool, `user` included, has one
slot). A task in its own worktree holds no files, so two tasks that share a
file can run at once: the second land replays its change onto the first.
`specflo task list --json` shows the ready set. `task reopen` frees the files
and slots of a task whose agent died, and saves a worktree's changes as a
patch. A task with no `Files` and no `Needs` is never held back.

The plan skill writes every plan so it can fan out, whatever the mode. It finds
every file each task touches, lists them in `Files`, and marks hardware, shared
environments and user-in-the-loop tasks with `--needs`. A `Depends on` edge
means a task needs another's result, never that two tasks share a file.

## pi subagents (`specflo agent`)

**Heavy work in progress.** The `specflo agent` surface is under active
development and changes constantly: commands, flags, the on-disk formats and
the behaviour itself can all move between releases, with no migration path.
Treat it as a preview to try, not a surface to build on.

The `specflo agent` group runs and controls headless pi coding agents. Each
agent is a detached host process that spawns `pi --mode rpc`, holds its stdio
as the sole owner, logs every event, and exposes a per-agent Unix socket for
control. A controller (you, or an orchestrating agent) drives it entirely
through the CLI; the host never interprets assistant text, so conventions like
completion phrases stay controller-side.
An rpc agent answers at its host's socket only: the host starts pi with
`SPECFLO_AGENT_SERVE=0`, so the specflo pi extension inside that pi serves no
control socket of its own.

### Verbs

- `specflo agent start <name> [--transport rpc|tui] [--cwd DIR] [--pi-cmd CMD] [--workspace ID] [--no-herdr] [--no-auto-answer]` -
  start an agent in `DIR`. The default transport `rpc` starts a detached host
  that spawns pi headless; both survive the invoking shell. `--transport tui`
  runs a real interactive pi in a herdr pane instead (see The TUI transport
  below); an unknown transport value is rejected naming the valid ones. Names
  are unique among live agents; a name is reusable once its host is stopped
  or dead. `--pi-cmd` overrides the spawned command (default `pi --mode rpc`,
  or plain `pi` under `--transport tui`; add `--model provider/id[:thinking]`
  there to pick a model). `--no-auto-answer` disables the dialog policy so a
  controller answers dialogs itself (rpc transport only).
- `specflo agent status <name> [--json]` - live-checked status: the socket is
  probed first, so a dead host reports `dead` instead of echoing stale state.
  `--json` carries the full snapshot (name, state, host and pi pids, context
  percent when known, herdr workspace/tab/pane ids, last activity) plus the
  state-dir paths (socket, events.jsonl, status.json).
- `specflo agent list [--json]` - every known agent with its live-checked
  state, each row carrying transport (`rpc`/`tui`) and ownership
  (`managed`/`adopted`). A dead session is never shown as attachable, and a
  cleanly exited session's leftover event log is not listed at all.
- `specflo agent prompt <name> "<text>" [--timeout S] [--no-wait] [--steer | --follow-up]` -
  send a prompt; block until the run settles and print the final assistant
  text to stdout. A working agent refuses a plain prompt (exit 10) unless
  `--steer` or `--follow-up` delivers it with pi streamingBehavior `steer` or
  `followUp`. `--no-wait` submits and returns immediately.
- `specflo agent wait <name> [--timeout S]` - block until the current run
  settles; exits 0 immediately when nothing is in flight.
- `specflo agent last <name>` - print the most recent final assistant text.
- `specflo agent reset <name>` - clear the agent's conversation context in
  place (pi `new_session`). The pi process, its working directory, its model
  and the prompt it was started with stay. A working agent refuses it (exit
  10).
- `specflo agent log <name> [--follow]` - print the agent's event log
  (events.jsonl); `--follow` streams new events as they land. For the
  holder of a lease it begins where the lease was bound.
- `specflo agent stop <name> [--timeout S]` - stop follows ownership. An rpc
  agent gets the v1 graceful stop: abort any in-flight run, terminate pi then
  the host (escalating to kill after a bounded grace period), release the
  herdr registration, and write the final `stopped` state with events.jsonl
  and status.json retained. A managed tui agent gets SIGTERM to the recorded
  pi pid; the extension's own shutdown removes the socket and record. An
  adopted session is never killed: stop detaches - it removes specflo's
  record, leaves pi running, says so, and exits `13`.

`status`, `prompt`, `wait`, `last`, `reset`, `log` and `stop` take
`--lease-token` for an agent leased from a daemon's pool; see
**[Leases](https://github.com/TacoTakumi/specflo/blob/main/DAEMON.md#leases)**. An agent under no lease needs none.

### Exit codes

Consistent across every agent verb, and stated in each verb's `--help`:

| Code | Meaning |
|------|---------|
| 0    | success / run settled |
| 10   | agent busy (working; use `--steer` or `--follow-up`) |
| 11   | wait timeout |
| 12   | host unreachable or unknown agent |
| 13   | adopted session detached (`stop`; pi left running) |
| 1    | generic usage or error |

### The TUI transport (`--transport tui`)

`start --transport tui` runs a real interactive pi - the TUI a person sits
at - in a herdr pane, and makes it a controllable agent at the same time. No
host process exists on this path: the specflo pi extension inside the session
binds the same per-agent control socket the v1 host binds, writes the same
discovery record, mirrors run events to `events.jsonl`, and drives
`status.json` through the same lifecycle, so every verb above (and the
Python client under them) works identically against both transports. herdr
is required for a tui start - the pane is the transport - and the command
reports success only once the socket accepts a connection; on timeout
(`SPECFLO_AGENT_START_TIMEOUT` overrides the bound) the pane is closed and
the exit code is `11`.

A controller and a human share the one session: socket prompts and typed
input land in the same transcript, in order. A plain socket prompt against a
streaming session is refused (exit 10); `--steer` and `--follow-up` deliver
with pi's streaming behaviors. For a managed pane the extension pushes real
lifecycle state into herdr - working during a run, idle at settle, blocked
while a UI prompt is open - and a blocking dialog also flips `status.json`
to `needs-attention` with the prompt kind and title in the event log, so a
controller can see the session is waiting and answer the dialog with pane
keystrokes (`herdr pane send-keys <pane> ...`).

Serving is on for every pi session with the extension loaded, so a session
someone started by hand is discoverable and adoptable: it appears in `list`
as `tui adopted`, named by its cwd basename (pid-suffixed on collision), and
can be attached with `status`, `prompt`, `wait`, `last`, and `log` like any
agent. Disable serving entirely with `SPECFLO_AGENT_SERVE=0` (or `off`), or
a `serve.off` marker file in the state base dir. On clean pi exit the socket
and record are removed and the event log is retained; a record left by an
unclean death is probed, never trusted - `list` marks it dead, and a new
session for the same identity replaces it.

### herdr placement and degraded mode

When the `herdr` CLI is installed and its server answers, `start` creates or
reuses a workspace labeled by the `agent_space` config key (default `agents`),
creates one tab labeled with the agent name, and runs the host inside that
tab's pane. The pane shows a live human-readable transcript - submitted
prompts, streamed assistant text, tool executions, dialog answers, and state
changes, never raw protocol frames - and accepts no input. The host pushes
real agent state into herdr (`herdr pane report-agent`) across the lifecycle,
so `herdr agent list` mirrors it without heuristic detection, and releases the
registration on stop. `start --workspace <id>` places the tab in the given
workspace instead of the `agent_space` one.

When herdr is unavailable (not installed, or the server is down) or
`--no-herdr` is passed, `start` proceeds headless with exactly one warning on
stderr naming the degradation; everything else works the same, with the
transcript going to `host.log` in the agent's state dir.

Per-agent state lives under `~/.specflo/agents/<name>/` (override with
`SPECFLO_AGENT_STATE_DIR`): the control socket, an append-only timestamped
`events.jsonl`, and an atomically updated `status.json` snapshot.

Dialogs from pi extensions are auto-answered by policy: confirm affirmed,
select gets the first policy-safe option, input/editor get a policy nudge, and
a dialog matching the danger pattern is cancelled. Every dialog and answer is
logged; past a per-run flood threshold the agent flips to `needs-attention`
and auto-answering stops for that run.

## The daemon and the agent pool

`specflo serve` runs a daemon that hosts projects, products and their work
items, with a web UI for the requester and the developer. The daemon can also
hold an agent pool: a roster of pi agents that orchestrators in any checkout
lease by name. Both are a heavy work in progress.
[DAEMON.md](https://github.com/TacoTakumi/specflo/blob/main/DAEMON.md) describes them.

### Users and sign-in

Every person who uses a daemon is a named user with one role, `developer` or
`requester`. The commands run on the daemon's machine and find the root as
`specflo serve` does:

- `specflo serve user add <name> --role developer|requester [--password]` - add a user. `--password` asks for a password twice at the terminal; without it the user can sign in only through a reverse proxy. A name is 1 to 64 characters of `a-z 0-9 . _ - @`, starts with a letter or digit and is stored lowercase. `agent` is reserved.
- `specflo serve user list` - each user with their role, whether a password is set and how many tokens they hold.
- `specflo serve user passwd <name> [--clear]` - set a new password and end the user's sessions. `--clear` removes the password, so the user signs in only through the proxy.
- `specflo serve user role <name> developer|requester` - change the role; it applies at the user's next request.
- `specflo serve user remove <name>` - remove the user with their tokens and sessions.
- `specflo serve user token add <user> --name <name>` / `token list <user>` / `token revoke <user> <name>` - mint, list and revoke a user's personal API tokens.

A browser signs in with the user name and password. A session lasts 12 hours
from its last use, is kept in the daemon's state store and survives a restart;
the header has a sign-out control. After 5 failed password sign-ins in 15
minutes, password sign-in for that user is locked for 15 minutes;
`serve user passwd` clears the lock. The header names the signed-in user and
links to their user page (`/user`), which holds the theme switch, the user's
personal tokens and, for a session signed in with a password, the change
password form.

A personal API token starts with `sfp_`, prints once, and the daemon keeps only
its SHA-256. A signed-in user creates, lists and revokes their own tokens on
their user page (`/user`). A token acts as its owner, with the role read at each
request. Register a daemon in a checkout with your own token:
`specflo remote add <name> <url> --token <secret>`. The agents the daemon
starts get tokens the daemon mints itself, one per seat, owned by the built-in
`agent` principal.

The developer role alone manages the pool (definitions, teams, reload),
releases leases from the pool page, uses consoles, runs the bench and sets a
project's egress pin. The `Actor` lines, the gate records, the chat and
`audit.jsonl` record the user's name and role.

A daemon root with a `tokens.json` from an older release is migrated on first
start: its agent tokens move into the state store, its requester and developer
tokens are dropped, and the file is renamed to `tokens.json.migrated`. Shared
human tokens stop working, so add each person with `serve user add`, give them
a personal token, and update each existing `remote add` registration with it.

### Reverse-proxy sign-in

A daemon behind a reverse proxy that already authenticates people (SSO, MFA)
can take the user name from a request header. It is off by default. Turn it on
in the daemon root's `.specflo/config.yaml`:

```yaml
auth_proxy: true
# The header the proxy sets to the authenticated user name.
auth_proxy_header: X-WEBAUTH-USER
# Socket addresses allowed to set it.
auth_trusted_proxies: [127.0.0.1, "::1"]
```

When a request comes from a trusted address and the header names a known user,
the daemon signs that user in without the form. From any other address the
header is ignored and the password form applies. A header that names a
different user than the live session switches the session. An unknown user is
refused (403) with a hint to run `specflo serve user add`; users are never
created from the header. The role always comes from the daemon's user record,
and the header never authenticates an API call. The daemon refuses to start
when `auth_proxy` is on and the trusted list is empty, holds a wildcard (`*`,
`0.0.0.0/0`, `::/0`) or holds an invalid entry.

An Apache example: authenticate, then set the header and pass the request on.
Use `RequestHeader set`, never `add`, so a header the client sent is replaced,
and unset it wherever no authentication runs. A proxy that terminates TLS must
also send `X-Forwarded-Proto: https`, which Apache does not do on its own; the
daemon reads it only from a trusted address, and without it the session cookie
is not marked Secure.

```apache
<Location "/">
    # Your authentication here, for example AuthType openid-connect
    Require valid-user
    RequestHeader set X-WEBAUTH-USER "%{REMOTE_USER}s"
    RequestHeader set X-Forwarded-Proto "https"
    ProxyPass "http://127.0.0.1:8741/"
    ProxyPassReverse "http://127.0.0.1:8741/"
</Location>
```

A trusted address is trusted for any user. Any process that can connect from
it can set the header and sign in as any user, proxy-only users included, and
then mint a personal token. With the loopback default that is any process on
the daemon host, the daemon's own project agents included. A second address on
the same host does not help, since a local process can connect from any local
address. Where agents share the host, run the proxy on another host and list
only its address, or keep `auth_proxy` off.

With the proxy on the same host, keep the daemon bound to the loopback address
(the default, or `specflo serve --bind 127.0.0.1`) so its port cannot be
reached directly. With the proxy on another host the daemon must listen on an
address that host can reach, so its port can be reached directly as well:
limit it with a firewall to the proxy's host. A user with a password who can
reach the port signs in without the proxy's own rules, such as MFA. The daemon
runs uvicorn without proxy-header rewriting, so the trust check sees the real
peer address.

## The config file

`.specflo/config.yaml` is written by `specflo init` and documents itself. Every
setting specflo has appears in it: live as `key: value` once set, commented out
at its shipped default while it is not, each under a one-line description. So
the file lists what you can change without a trip to the docs, and `config set`
is literally uncommenting a line you can already see.

```yaml
# Where project artifacts live, relative to the repo root.
projects_dir: docs/projects

# The project every command acts on; set it with `specflo switch`.
active_project: my-thing

# Default autonomy level for `specflo auto`: safe, autonomous or yolo.
# autonomy: safe

# Runaway backstop: the most passes one `specflo auto` run may take.
# auto_max_passes: 50

# Percent of the context window at which the pi extension arms clear-and-continue.
# context_threshold_percent: 25

# herdr workspace label where `specflo agent start` places agent tabs.
# agent_space: agents

# Transport a daemon starts a project's agent with: tui (pi in a herdr pane) or rpc.
# agent_transport: tui

# Review rounds a level may take before `review start` asks for --over-budget or a waive.
# review_max_rounds: 2

# Largest file `research ingest` takes, in MB; a bigger one is refused unread.
# research_source_limit_mb: 50

# Seconds a `research ingest` conversion may take before it is stopped and refused.
# research_convert_timeout: 120
```

It is your file, so specflo writes it conservatively:

- A comment you wrote, the key order you chose, and a key specflo has never
  heard of all survive every write.
- Reading never writes. `specflo status --json`, which the pi extension polls
  every turn, leaves the bytes identical.
- A config written before a setting existed gains it on the next write,
  commented out at its default, announced by one note on stderr naming what was
  added.
- A value the file sets but specflo cannot accept degrades to the shipped
  default with a warning, rather than breaking every command that loads it.
  `specflo config list` marks that key `(invalid, using default)`.

Edit the file by hand or go through `specflo config` - the commands validate
before writing, which the editor cannot.

## Session-start integration (clear-and-continue)

An agent can't clear its own context *or* remember what to do across a `/clear` -
the continuation must come from outside the conversation. `specflo hook reseed`
is that bridge: install it once (`specflo hook install`), and on a fresh start,
after a `/clear`, or when you resume a session, Claude Code reorients from the
on-disk checkpoint and **asks before resuming**, so you never re-explain where
you were. Because a `SessionStart` hook can re-ground the agent but cannot make
it speak first, the wiring also surfaces a short visible `systemMessage`
telling you what to type (e.g. `continue`) to start the hand-off.

The installed wiring is Claude Code's. The payload itself is portable: another
harness (opencode, OpenAI Codex, ...) can call the plain-text
`specflo hook reseed` from its own session-start mechanism and inject the
output as context. pi is the exception - it needs no wiring at all, because the
bundled pi extension injects the reseed itself (see
**[The pi extension](#the-pi-extension)**).

**Security posture:** the reseed injects only **trusted local state** - the
checkpoint is derived read-only from the project's own artifacts, never from
external or network input - so running it at session start is benign.

## The pi extension

For the [pi] coding agent, specflo goes further than the hook: a bundled pi
extension performs the clear itself. It is a thin driver by design - every
piece of state it acts on is the stdout of a `specflo` command, it keeps no
durable state of its own, registers no model-callable tool, and never blocks a
tool call. Install it once (pi 0.81+):

```bash
specflo extension install                  # -> ~/.pi/agent/extensions/specflo
specflo extension install --scope project  # -> ./.pi/extensions/specflo
```

pi discovers the directory on its own - nothing else to wire. Re-running is
safe: an up-to-date install is reported as current, a stale one is replaced
whole.

In an attended pi session:

- **Cold start.** When pi starts or resumes inside a specflo repo, the first
  turn is seeded with the `specflo hook reseed` payload, so the agent already
  knows where the project stands and asks before resuming. With no active
  project it injects nothing.
- **Arming.** At each turn's end the extension reads pi's own context-usage
  percent and arms once it reaches `context_threshold_percent` (default `25`,
  set in `.specflo/config.yaml`). Arming is not firing: the next specflo seam
  fires it, so the effective clear point is that percent plus one task's worth
  of context.
- **The seam.** While armed it watches `specflo status --json` for a safe
  point to clear: the phase advancing, or a task reaching done. A task merely
  in progress is never a seam, so in-flight work is never discarded.
- **The notice.** An armed seam produces one passive notice naming the current
  usage, the seam that fired, and the command to run. Nothing clears on its
  own in an attended session.
- **`/specflo-continue`.** Clears the session and reseeds the
  direct-continuation payload - the checkpoint plus the current task brief -
  so the fresh session carries straight on.

In an auto run:

- **`/specflo-continue auto`** starts or continues an unattended run from
  inside pi - the same explicit opt-in `specflo auto` is, counting a pass in
  the same run state - and delivers that pass's payload into a fresh session.
- **The unattended fire.** After one clear has run, every armed seam clears
  and reseeds by itself: the extension fetches the next `specflo auto` pass
  and delivers its payload verbatim. No dialog, no confirmation, no input.
- **Joining a run cold.** If the run was started outside pi (`specflo auto` in
  a terminal, then pi opened), the first armed seam prints a bootstrap notice
  asking you to type `/specflo-continue auto` once; every seam after that is
  unattended.
- **Stopping.** Loop control lives in the CLI, never the extension: on the
  kill switch (`specflo auto --off`), the pass cap, a stall, or project
  completion, nothing clears and the CLI's own stop directive is shown as a
  notice. `specflo auto --on` clears the kill switch again.

[pi]: https://www.npmjs.com/package/@earendil-works/pi-coding-agent

## Statusline

The active project in your statusline tells you where the agent is without
asking: `my-project:plan 0/5` in plan, or `my-project T-03 2/5` in execute
(the task in progress, then done over total). pi users get this segment from
the pi extension. For Claude Code, the repo ships two example scripts in
[examples/statusline](https://github.com/TacoTakumi/specflo/tree/main/examples/statusline):

- [specflo_segment.py](https://github.com/TacoTakumi/specflo/blob/main/examples/statusline/specflo_segment.py)
  prints only the specflo segment, so you can add it to the statusline you
  have. Call it with the session's directory (`specflo_segment.py "$dir"`), or
  import it from Python (`specflo_segment.segment(dir)`). It prints nothing
  outside a specflo repo, honours `SPECFLO_DIRECTORY` and `NO_COLOR`, and
  reads the files directly, so it adds no `specflo` call to each redraw.
- [claude_statusline.py](https://github.com/TacoTakumi/specflo/blob/main/examples/statusline/claude_statusline.py)
  is a full Claude Code statusline: the project directory, the specflo
  segment, the model and effort, context use, the cache-read share, and the
  5-hour and 7-day quota with their reset times. It imports
  `specflo_segment.py` from its own directory.

To use the full one, copy both files to `~/.claude/`, make them executable,
and add this to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/claude_statusline.py"
  }
}
```

A project hosted on a daemon has no local files, so the segment shows nothing
for it.

## Skills

specflo ships its eleven workflow skills inside the package and installs them into
whatever agent harness it finds on your machine (Claude Code, pi, Hermes,
opencode). Let the CLI do it - no copying or symlinking by hand:

```bash
specflo skills install     # into the user skills dir of every detected harness
specflo skills status      # what is installed, and whether it is current
specflo skills update      # bring stale installs up to the bundled version
specflo skills uninstall   # remove the ones specflo's stamp owns
```

On an interactive terminal, `install` asks which harnesses and scopes to use;
pass `--no-input` (with `--harness`/`--scope`) to script it. Every verb takes
`--scope user|project` - `user` (the default) is the harness's user-level skills
dir such as `~/.claude/skills/`, and `project` is the repo-local one such as
`./.claude/skills/` - plus a repeatable `--harness NAME[:SCOPE]` to target one
harness instead of all detected ones.

Each install is a plain copy carrying a specflo provenance stamp, so `update` and
`uninstall` only ever touch skills specflo itself installed, and a skill you have
edited locally is never overwritten without `--force`. When an installed skill
falls behind the bundled version, any specflo command prints a single advisory
line on stderr pointing at `specflo skills update`. It is notice-only: it never
prompts, never updates anything, and never changes the exit code. Silence it by
setting `CI` or `AGENTSQUIRE_NO_UPDATE_CHECK`.

The eleven skills:

- **`specflo-brainstorm`** (`skills/specflo-brainstorm/SKILL.md`) - drives the brainstorm phase over the CLI above (one question at a time, captures decisions, validates, hands off to the spec phase).
- **`specflo-spec`** (`skills/specflo-spec/SKILL.md`) - drives the spec phase (synthesize testable `REQ-NN` requirements from the brainstorm, validate, hand off to the plan phase).
- **`specflo-plan`** (`skills/specflo-plan/SKILL.md`) - drives the plan phase (decompose the validated spec into dependency-ordered, testable `T-NN` tasks, validate, hand off to the execute phase).
- **`specflo-execute`** (`skills/specflo-execute/SKILL.md`) - drives the execute phase (work tasks one at a time with `task show`/`task start`/`task done`, run the final whole-branch review in fresh context and record it with `review start`/`review done`, fix each blocker and should-fix item with a task that names it in `--fixes`, defer or reject an item only on the user's say, open harden rounds for a deeper look, validate with `validate execute`, complete the project with `advance`).
- **`specflo-quick`** (`skills/specflo-quick/SKILL.md`) - works a quick-level project: fills the brief through the CLI, does the work, records proof, makes one commit, completes the project with no review round, and offers `specflo level fast` when the work outgrows one check.
- **`specflo-harden`** (`skills/specflo-harden/SKILL.md`) - works a harden-level project: starts it, writes the brief's Scope, Focus and Stop when, runs harden rounds with a fresh-context reviewer, turns their blocker and should-fix findings into fix tasks, and stops on the user's say, with the whole suite run once.
- **`specflo-research`** (`skills/specflo-research/SKILL.md`) - a research subagent the `specflo-brainstorm` skill dispatches to ground decisions in current facts: an upfront **landscape scan** (what tools/SDKs/clients/frameworks already exist) plus **opportunistic** assumption-checks. Wiki-integrated - searches the Agent Wiki first and saves findings back (soft dependency).
- **`specflo-research-project`** (`skills/specflo-research-project/SKILL.md`) - drives a session of a research-level project: opens with `specflo research recap`, asks one question at a time (no suggested answer on a knowledge question, a recommendation on a choice), records each source, finding, question, term and decision with the CLI as it lands, keeps the user's material out of outgoing searches, and ends with `specflo research session close`. Not the `specflo-research` subagent above, which does a one-off fact check inside a brainstorm.
- **`specflo-shelve`** (`skills/specflo-shelve/SKILL.md`) - recognizes "park this for now" / "let's pick that back up" and maps them to `specflo shelve` and `specflo resume`, so a project can be set aside and reclaimed without losing its phase or artifacts.
- **`specflo-auto`** (`skills/specflo-auto/SKILL.md`) - recognizes an unattended-run intent ("auto mode", "autopilot", "keep going without me") and maps it to `specflo auto`, then follows the emitted payload. Thin by design: the CLI carries the loop, autonomy policy, and guardrails; the skill only triggers it and hands the directives to the loop.
- **`specflo-agent`** (`skills/specflo-agent/SKILL.md`) - drives headless pi subagents through the `specflo agent` CLI: start a worker, send it prompts, monitor it and stop it. It is for delegating work to pi, not for the pipeline phases. A preview, like the [`specflo agent`](#pi-subagents-specflo-agent) surface it uses.

### Working on the skills themselves

If you are editing specflo's own skills from a checkout, symlink them instead so
your edits take effect immediately. specflo recognizes a symlinked live-edit
install and stays quiet about updates for it:

```bash
ln -s "$PWD/skills/specflo-brainstorm" ~/.claude/skills/specflo-brainstorm
```

## Install from source

From a checkout, with [uv](https://docs.astral.sh/uv/) installed
(`curl -LsSf https://astral.sh/uv/install.sh | sh`):

```bash
uv tool install .              # install `specflo` globally, from the repo root
uv tool install --reinstall .  # update after pulling changes
uv tool uninstall specflo      # remove
```

`pipx install .` and `pip install .` work the same way if you prefer them.
A machine that runs the daemon needs the web stack too: `uv tool install '.[serve]'`
(or `pip install 'specflo[serve]'`). A machine where a research project takes in
PDF or Word documents needs the converters: `uv tool install '.[docs]'` (or
`pip install 'specflo[docs]'`), which adds pypdfium2, mammoth and markdownify
and nothing else; without it `specflo research ingest` refuses a `.pdf` or
`.docx` naming the extra, and every other command runs as before.

To build distributables instead, `uv build` writes the wheel and sdist into
`dist/`; install the wheel anywhere with
`uv tool install ./dist/specflo-<version>-py3-none-any.whl` (no source checkout
needed).

## Development

```bash
uv sync                    # create the venv and install deps (incl. dev group)
uv run pytest              # run the tests, on 8 parallel workers
uv run pytest -n 0         # run the tests in one process, for -s output or pdb
uv run specflo --help      # run the CLI without installing it
```

`uv run pytest` runs the suite on 8 pytest-xdist workers (`-n 8` in the pytest
config in `pyproject.toml`). `-n 0` on the command line turns that off, which
`-s` output and pdb need. Each test has a 120 s limit (pytest-timeout, signal
method): a test that hangs fails at its limit, its fixture teardown runs, and
the run goes on.

The tests wait through one shared helper, `tests/waits.py`. On a slow or
loaded machine, set `SPECFLO_TEST_WAIT_SCALE` to a number above 0 (default 1)
to multiply every limit and settle pause of that helper, for example
`SPECFLO_TEST_WAIT_SCALE=3 uv run pytest`.

## Model bench

`specflo bench` is a model bench that measures how well a local model drives
specflo; install it with `pip install 'specflo[bench]'` and start with
`specflo bench init`. It runs a seeded specflo project (quick, fast or full) unattended
under pi or Claude Code against a model served by llama-swap, grades the
final tree with a held-out test suite and graphs the pass rates with behaviour
metrics. Each comparison changes one variable: the model, the engine or the
harness. See [BENCH.md](https://github.com/TacoTakumi/specflo/blob/main/BENCH.md)
for how it works and how to run it.

## License

[GPL-3.0-or-later](https://github.com/TacoTakumi/specflo/blob/main/LICENSE). Copyright (C) 2026 TacoTakumi.
