# Due dates for tasks

I want to give tasks a due date and see which ones are overdue.

## What I need

- A task can have an optional due date. In code it is `Task.due`, a string in
  `YYYY-MM-DD` form, or `None` when there is no due date (the default). A due
  date that is not a real calendar date in that form (for example `2026-13-01`
  or `next week`) raises `ValueError("invalid due date: <value>")`.
- The due date is saved in the task file under the key `"due"` (`null` when
  there is none). Task files written by the current version, which have no
  `"due"` key, must still load; their tasks have no due date.
- `TaskStore.add(title, priority="normal", due=None)` takes the due date.
- `TaskStore.overdue(today)` takes a `datetime.date` and returns the pending
  tasks whose due date is before `today`, sorted by due date and then by id.
  Done tasks and tasks without a due date are never overdue. A task due on
  `today` is not overdue.
- `tinytodo add TITLE --due YYYY-MM-DD` sets the due date. A bad date prints
  `error: invalid due date: <value>` to stderr and exits with status 1, and no
  task is added.
- In `list` output, a task with a due date ends in ` due YYYY-MM-DD`, after the
  priority if there is one. For example: `[ ] 2 Pay rent (high) due 2026-10-01`.
  Tasks without a due date print as they do today.
- `tinytodo list --overdue` prints only the overdue tasks, in the same order
  as `TaskStore.overdue`, using the normal line format. It uses today's date,
  or the date given with `--today YYYY-MM-DD` (so it can be tested). A bad
  `--today` value is an error in the same way as a bad due date.

Everything that works today should keep working the same way. Please add tests
for the new behaviour.
