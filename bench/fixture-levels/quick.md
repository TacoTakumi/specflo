# Show a message when the task list is empty

Right now `tinytodo list` prints nothing at all when there is nothing to show,
which looks like the command did not work.

When `list` (with or without `--all`) has no tasks to print, it should print
exactly one line, `No tasks.`, and exit with status 0. This covers an empty or
missing task file, and also `list` without `--all` when every task is done.
When there is at least one task to print, the output stays exactly as it is
today.

Please add a test for it.
