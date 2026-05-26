# `gg rbt-sync --adopt` — reconcile against another branch's reviews

**Status:** approved
**Date:** 2026-05-26

## Problem

`reviews.db` rows are keyed by branch name. A common workflow is
broken by this:

1. `branchA` (tracking `origin/main`) has a series posted to RB.
2. The user creates `branchB` from `branchA`, also tracking
   `origin/main`, then modifies the commits (amend, reorder, drop,
   add). The goal is to keep `branchA` as the original reference and
   continue review work on `branchB`.
3. `gg rbt-sync` on `branchB` reads its own (empty) rows and falls
   into `--new`, creating brand-new RB reviews — losing all existing
   review threads.

The reconcile matcher itself is fine: given `branchA`'s rows as the
"old" set, it would produce the right plan (KEEP / UPDATE / DISCARD /
CREATE) for `branchB`'s current commits. The only missing piece is a
way to point reconciliation at a different branch.

## Goal

Add `--adopt SRC` to `gg rbt-sync`. When set, reconcile against
`SRC`'s rows instead of the current branch's, and save the resulting
entries under the current branch's name.

## Non-goals

- Aliasing in the DB schema (e.g. a "branchB inherits from branchA"
  table). Persistent automatic inheritance hides state and creates
  edge cases (chains, cycles, renames). `--adopt` is an explicit
  one-shot.
- Adding adopt to `gg rbt`. `gg rbt-sync` is the reconcile path; the
  one-shot posting path doesn't need it.
- Modifying `SRC`'s rows. After adopt, `branchA`'s DB still references
  the (now updated/discarded) review IDs — that's the price of keeping
  the original branch as a reference. Documented in the README.

## Surface

Two new flags on `gg rbt-sync`:

| Flag | Meaning |
|---|---|
| `--adopt SRC` | Reconcile against `SRC` branch's DB rows; save new entries under the current branch. |
| `--adopt-overwrite` | Only valid with `--adopt`. Overwrite the current branch's existing rows (otherwise we refuse, to protect an in-progress series). |

**Why not reuse `-f/--force`?** It already means "re-post every
matched commit, ignoring diff hash". Overloading it would couple two
unrelated decisions.

### Mutual exclusions / validation

| Condition | Behavior |
|---|---|
| `--adopt-overwrite` without `--adopt` | Friendly error |
| `--adopt` with `--new` or `--close` | Friendly error (semantic opposites) |
| `--adopt SRC` where `SRC == current branch` | Friendly error |
| `SRC` has no rows in the DB | Friendly error: "no reviews to adopt from branch '<SRC>'" |
| Current branch has rows and `--adopt-overwrite` not set | Friendly error: "branch '<B>' already has N reviews; pass --adopt-overwrite to replace" |

All errors print one line to stderr and return exit code 1. No
tracebacks.

### Dry-run semantics

`--dry --adopt SRC` reads `SRC`'s rows, runs reconcile, prints the
plan, writes nothing — same dry semantics as today. No RB calls.

## Behavior table

| Scenario | Result |
|---|---|
| Adopt with identical commits | All `KEEP`. No RB calls. |
| Adopt with amended commits | Subject-fuzzy matches → `UPDATE` against existing review IDs. |
| Adopt with reordered + amended | `UPDATE`; `--renumber` works as today. |
| Adopt with a commit dropped | `DISCARD` (closes that RB review). `SRC`'s DB row not touched. |
| Adopt with a commit added | `CREATE`. |
| Adopt + `-p/--publish` | Works (publish flag flows through `_execute`). |
| Adopt + `-i/--interactive` | User edits the plan with `SRC`'s entries as the baseline. |
| Adopt + `-f/--force` | Turns `KEEP` into `UPDATE`. |
| Adopt + `--dry` | Plan shown, DB unchanged for both `SRC` and target. |

## Implementation

All changes confined to `src/gg/sync.py` (plus tests and README). The
matcher, `review_store`, `gg db`, and `gg rbt` are untouched.

### Argparse

In `add_parser`:

```python
p.add_argument("--adopt", metavar="SRC", default=None,
               help="reconcile against SRC branch's reviews; save as current branch")
p.add_argument("--adopt-overwrite", action="store_true",
               help="(with --adopt) overwrite current branch's existing rows")
```

### Validation block

In `run()`, after `branch_name = git.branchname(...)` and before the
upstream resolution:

```python
if args.adopt_overwrite and not args.adopt:
    print("[gg] --adopt-overwrite requires --adopt", file=sys.stderr)
    return 1
if args.adopt and (args.new or args.close):
    print("[gg] --adopt is incompatible with --new/--close", file=sys.stderr)
    return 1
if args.adopt == branch_name:
    print(f"[gg] cannot adopt from current branch '{branch_name}'", file=sys.stderr)
    return 1
```

### Loading the "old" set

Replace the existing `old = review_store.load_reviews(branch_name, ...)`
block (~line 232) with:

```python
source_branch = args.adopt or branch_name
old = review_store.load_reviews(source_branch, cwd=cwd)

if args.adopt:
    if not old:
        print(f"[gg] no reviews to adopt from branch '{args.adopt}'",
              file=sys.stderr)
        return 1
    existing = review_store.load_reviews(branch_name, cwd=cwd)
    if existing and not args.adopt_overwrite:
        print(
            f"[gg] branch '{branch_name}' already has {len(existing)} reviews; "
            f"pass --adopt-overwrite to replace",
            file=sys.stderr,
        )
        return 1
elif not old and not args.new:
    # Existing auto-new fallback, unchanged.
    print("[gg] No existing reviews; posting as a fresh series.", file=sys.stderr)
    args.new = True
```

### No downstream changes

`_execute()` already tags new entries with `branch=branch_name`
(current branch), and `save_reviews` deletes-then-inserts under that
key. So `SRC`'s rows stay intact and the current branch's rows get
the new series. The `diff_hashes` table for the current branch gets
repopulated by the normal `diff_cache.save_hashes(...)` call at the
end of `run()`. No explicit copy of `diff_hashes` is needed.

## Testing

New tests in `tests/test_rbt_sync.py`:

1. **`test_adopt_keep_unchanged`** — On `branchB` (copy of A's
   commits), `gg rbt-sync --adopt branchA -d` produces an all-`keep`
   plan; no new RB calls.
2. **`test_adopt_update_amended`** — Amend `branchB`'s commits; run
   `gg rbt-sync --adopt branchA` (non-dry); verify `rbt post -r <ID>`
   was called against the same review IDs as A's DB rows, and that
   `branchB`'s DB row points to those same IDs.
3. **`test_adopt_self_branch_errors`** — `--adopt <current>` returns
   1, prints friendly error, no traceback.
4. **`test_adopt_empty_source_errors`** — `--adopt <name-with-no-rows>`
   returns 1, mentions the source name.
5. **`test_adopt_conflict_refuses_without_overwrite`** — Pre-populate
   `branchB`'s DB rows, then `--adopt branchA` without overwrite →
   refuses, suggests `--adopt-overwrite`.
6. **`test_adopt_overwrite_proceeds`** — Same setup, with
   `--adopt-overwrite` — proceeds and replaces.
7. **`test_adopt_incompatible_with_new`** — `--adopt X --new` →
   friendly error.
8. **`test_adopt_dry_writes_nothing`** — `--adopt branchA --dry` →
   plan shown, both `branchA` and current branch DB rows unchanged.

## Documentation

- Add a row to the `gg rbt-sync` table in `README.md`:
  ```
  | `git gg rbt-sync --adopt branchA` | Reconcile against `branchA`'s reviews and save under the current branch |
  ```
- Add a short subsection under "Typical workflows" explaining the
  branchA-as-reference flow: create `branchB`, modify commits,
  `gg rbt-sync --adopt branchA`. Mention the trade-off: after adopt,
  `branchA`'s DB rows still reference the (now updated or discarded)
  review IDs — treat `branchA` as frozen.
- No changes to `CLAUDE.md` (architecture stays the same).
