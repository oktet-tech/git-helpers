# `--progress` for `gg rbt-sync`

## Problem

`git rbt` maps to `gg rbt-sync` (sync.py). A dry run (`-d`) prints a numbered
plan table (`format_plan` in `sync_plan.py`) and stops. A **real** run prints
that same table once, then `_execute()` runs `post_one` / `publish_one` /
`close_*` — none of which print anything unless `--verbose`, which dumps raw
`rbt` output. So a real run goes from the table straight to silence until the
final `Synced: …` summary line. On a 34-commit series there is no way to see
which entry is being worked on right now.

The sibling command `gg rbt` (rbt.py) already solved this with a `--progress`
flag that prints one prose line per patch. This spec brings `gg rbt-sync` to
parity.

## Goal

Add a `--progress` flag to `gg rbt-sync` that prints **one prose line per
entry** as a real run executes, in the style of `gg rbt`.

## Flag & semantics

- New parser argument in `sync.py`: `--progress` (long-only).
  - `-p` is already `--publish`; `gg rbt` likewise has no short form for it.
  - Help text: `print one line per action`.
- `show_progress = args.progress or args.verbose` — verbose implies progress,
  exactly as `rbt.py:57`.
- Only affects real runs. `--dry` prints the full plan table and returns before
  `_execute()`; dry-run output is unchanged.
- Progress lines go to **stdout** (matching `gg rbt`). The `Synced: …` summary
  stays on stderr as today.

## Output vocabulary

Threaded into `_execute()`, which gains a `progress: bool` parameter. Reuse the
existing `_BOLD` / `_RESET` constants and `flush=True`, and the `[i/N]` counter
already produced by `assign_numbers`. Every entry emits a line:

| Action kind | Line(s) |
|---|---|
| `DISCARD` | `discard r/<id>: <subject>` |
| `SKIP` (kept in DB) | `skip: <subject>` |
| `KEEP`, no-op | `keep (unchanged): <subject>` |
| `KEEP` + publishing an unpublished draft | `publish (unchanged): <subject>` then `  -> published r/<id>` |
| `CREATE` | `posting [i/N]: <subject> ...` then `  -> created r/<id>` |
| `UPDATE` / `KEEP_DEP` (dep refresh) / renumber re-post | `posting [i/N]: <subject> ...` then `  -> updated r/<id>` |

- The `posting …` line prints **before** the network call, so a hang is visible
  at the right entry.
- The `  -> created/updated/published r/<id>` line prints **after**, using the
  id returned by `post_one` (falling back to the existing entry's id for
  updates). This result line is the one enhancement over `gg rbt`, which only
  prints the "before" line.
- Subjects use the same value shown in the plan (the commit summary /
  old-entry subject), not the prefix-stripped form.

## Execution order

`_execute()` runs in two phases: all discards first (phase 1), then every
non-discard action in series order (phase 2). Progress lines therefore print as:
discards grouped up front, then the rest in `[i/N]` order. This reflects actual
execution order and is accepted in preference to restructuring `_execute()` to
match the interleaved dry-run table.

## Non-goals

- No change to error handling or `_execute()`'s existing partial-failure
  behavior.
- No change to the plan table, the summary line, or `rbt.py`.
- No new short flag.

## Testing

Extend the `rbt-sync` tests using the `rbt_mock` fixture:

1. `--progress` on a mixed plan (keep / create / update / discard) emits the
   expected per-action lines, including the `-> created r/<id>` result lines.
2. A plain real run (no `--progress`, no `--verbose`) stays quiet between the
   plan table and the `Synced: …` summary.
3. `--verbose` alone also produces the progress lines (verbose implies
   progress).
