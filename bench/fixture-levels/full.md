# Tags, editing, search, stats and export

tinytodo is getting used for more than a handful of tasks, so I need a set of
features to organise and get data out of it. All existing commands and output
must keep working as they do today unless something below changes them.

## Tags

- A task has a list of tags, `Task.tags`, empty by default. A tag is made of
  lowercase letters, digits and hyphens, and starts with a letter or digit.
  Tags given in uppercase are lowercased. Anything else raises
  `ValueError("invalid tag: <tag>")`, showing the tag as given.
- A task keeps its tags in the order they were added, without duplicates.
- Tags are saved in the task file under the key `"tags"`. Task files written
  by the current version, which have no `"tags"` key, must still load; their
  tasks have no tags.
- `TaskStore.add(title, priority="normal", tags=())` takes the tags.
- `tinytodo add TITLE --tag home --tag errands` adds tags (`--tag` can repeat).
- `tinytodo tag ID TAG [TAG ...]` adds tags to a task and prints
  `Tagged task <id>: <title>`. Adding a tag the task already has is not an error.
- `tinytodo untag ID TAG [TAG ...]` removes tags and prints
  `Untagged task <id>: <title>`. Removing a tag the task does not have is not
  an error.
- In every listing, each tag is shown at the end of the line as ` #<tag>`, after
  the priority. For example: `[ ] 4 Buy milk (low) #home #errands`.
- `tinytodo list --tag TAG` shows only tasks that have that tag (it combines
  with `--all`).

## Editing

- `TaskStore.edit(task_id, title=None, priority=None)` changes the title
  and/or priority of a task and returns it. The same validation applies as when
  adding a task.
- `tinytodo edit ID [--title TITLE] [--priority low|normal|high]` makes the change and prints
  `Edited task <id>: <title>` with the new title. With neither option it prints
  `error: nothing to edit` to stderr and exits with status 1.

## Sorting

- `tinytodo list --sort priority` lists high first, then normal, then low, and
  by id within each priority. `--sort id` is the default and is today's order.

## Search

- `TaskStore.search(text)` returns all tasks, done or not, whose title contains
  `text`, ignoring case, in id order.
- `tinytodo search TEXT` prints the matches in the normal line format, or the
  single line `No matches.` when there are none (exit status 0 either way).

## Stats

- `tinytodo stats` prints exactly these four lines:

      Total: <all tasks>
      Done: <done tasks>
      Pending: <pending tasks>
      High priority pending: <pending tasks with priority high>

## Export

- `tinytodo export --format csv` prints all tasks (done ones too) in id order
  as CSV with the header `id,title,priority,done,tags`. `done` is `yes` or
  `no`, and `tags` is the tags joined with `;` (empty when there are none).
  Use the standard CSV quoting for titles with commas or quotes, and `\n` line
  endings.
- `tinytodo export --format markdown` prints `# Tasks`, a blank line, and then
  one line per task in id order: `- [ ] <title>` or `- [x] <title>`, followed
  by the same priority and tag suffixes as `list` uses. For example:
  `- [x] Pay rent (high) #bills`.
- `--format` is required; any other value is a usage error (exit status 2).

Every error from these commands goes to stderr as `error: <message>` with exit
status 1, the same as today; an unknown task id prints
`error: no task with id <id>`. Please add tests for all of it.
