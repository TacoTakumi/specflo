# tinytodo

A small command-line to-do list. Tasks live in a JSON file. The standard
library is the only dependency.

## Usage

    python -m tinytodo add "Buy milk"
    python -m tinytodo add "Pay rent" --priority high
    python -m tinytodo list
    python -m tinytodo list --all
    python -m tinytodo done 1
    python -m tinytodo remove 2

The task file is `todo.json` in the current directory. Set `TINYTODO_FILE`
or pass `--file PATH` (before the command) to use another file.

`list` shows pending tasks, one per line: `[ ] 1 Buy milk`. A done task
shows `[x]`. A task whose priority is not `normal` ends in ` (high)` or
` (low)`. `list --all` also shows done tasks. Tasks are listed in id order.

Errors go to stderr as `error: <message>` and the exit status is 1.

## Layout

- `tinytodo/model.py` - the `Task` record and its validation.
- `tinytodo/store.py` - `TaskStore`, which loads and saves the JSON file.
- `tinytodo/cli.py` - the command line (`main(argv) -> int`).

## Tests

    python -m pytest
