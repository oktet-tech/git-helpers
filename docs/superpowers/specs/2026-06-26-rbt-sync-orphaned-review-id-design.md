# Auto-repair commits with a lost (empty) review_id in `gg rbt-sync`

## Problem

When a commit's original `rbt post` fails mid-flight, its row in
`reviews.db` is saved with an **empty `review_id`** (`""`), while later
commits in the series may have posted successfully — recording the empty
string as their stored predecessor.

On a later `gg rbt-sync` run the diff is unchanged, so:

- **The matcher** (`matcher.py`) classifies the orphaned commit as `KEEP`.
- `_mark_dep_updates` keys the dependency chain on `review_id`, so the dead
  `""` link looks self-consistent (the successor's stored predecessor
  genuinely is `""`). Nothing downstream is flagged. Everything stays
  `keep`.
- **Execution** (`sync.py`, the `KEEP` no-op branch) then, under `-p`, calls
  `publish_one("")` — publishing an empty review id. The successor's
  dependency on ReviewBoard still points at the dead link.

Observed in a 35-commit series: position 29 shows `keep yes r/` (empty id),
and the run would try to publish an empty id and leave position 30's
dependency broken.

The codebase already recognizes this failure mode — `_execute` computes
`needs_fresh_post = CREATE or (old_entry and not old_entry.review_id)` with
the comment *"entries with an empty review_id are recovery cases (a previous
post failed mid-flight)"* — but that path only runs for `UPDATE`/`KEEP_DEP`,
never for a plain `KEEP`. An unchanged commit whose post failed slips
through as a no-op.

## Goal

Automatically repair an orphaned (empty `review_id`) entry: **re-post it as
a fresh review, refresh the immediate successor's dependency to point at the
new id, and publish both under `-p`** — with the repair visible in the
`-d` dry-run plan.

## Decisions

- **Automatic, not flag-gated.** An empty `review_id` is unambiguously
  broken state; there is no scenario where keeping it as-is is correct. The
  repair still surfaces in the dry-run plan, so the user sees and approves
  it before executing.
- **Fixed in the reconciliation layer** (`matcher.py`), so the repair shows
  in `-d`, rather than in `_execute` (which `-d` never reaches).
- **Reuse existing machinery.** No new `ActionKind`, no new flag. Reclassify
  to existing `UPDATE` / `KEEP_DEP`; let `_execute`'s `needs_fresh_post` and
  `depends_on` threading do the rest.

## Component 1 — Force a re-post of the orphaned entry

`matcher.py`, the action-building loop in `reconcile`.

When a commit matches a stored entry whose `review_id` is empty, classify it
`UPDATE` regardless of diff/subject. Place the check so it takes precedence
over the `KEEP` classification: a matched entry with `not entry.review_id`
becomes `UPDATE`.

`_execute` already treats an empty-id `UPDATE` as `needs_fresh_post`: it
posts a fresh review (no `-r`), captures the new id, and under `-p`
publishes it. The fresh-post path requires reviewers; they come from `-U`
when supplied, otherwise are inherited from the predecessor via
`rb_api.fetch_reviewers` (existing behavior).

## Component 2 — Repair the successor's dependency

`matcher.py`, `_mark_dep_updates`.

While threading the predecessor chain, treat an entry with an empty
`review_id` as *"its id will change"*: after processing such an entry, set
the threaded predecessor to a module-level sentinel that compares unequal to
every stored predecessor value (including `""` and `None`). The next
non-discard entry then sees `prev != expected_pred` and is marked
`KEEP_DEP` with `needs_dep_update=True`.

At execution the successor takes the re-post branch (the `KEEP` no-op branch
requires `not action.needs_dep_update`), re-posting with `-r <its own id>`
and `depends_on=<the repaired patch's new id>`, and publishing under `-p`.

This generalizes to runs of consecutive empty-id entries (each is
re-posted; the first entry with a real id after the run is marked
`KEEP_DEP`) and removes a latent `old_pred[""]` key-collision that occurs
when more than one entry has an empty id.

The sentinel is local to `_mark_dep_updates` — it is only used for
mark-time comparison and never reaches `_execute`'s real `depends_on`
threading.

## Component 3 — Plan table display

`sync_plan.py`, `format_plan`.

Render an empty old-entry review id as `r/(lost)` instead of a bare `r/`,
so the dry-run plan makes the orphan obvious. This applies wherever the
table prints `r/{old_entry.review_id}` for an entry whose id is empty. A
brand-new `CREATE` (no `old_entry`) keeps its existing `--`; `r/(lost)`
specifically marks "had a review, lost the id."

Resulting `-d` plan for the observed case:

```
[29/35]  update    yes   r/(lost)  feat(mcp): add summarize_flakiness...
[30/35]  keep+dep  yes   r/19098   fix(mcp): count missing stability...
```

## Data flow

`reviews.db` (pos29 `id=""`) → `reconcile` → pos29 `UPDATE`
(`needs_fresh_post`), pos30 `KEEP_DEP` → dry-run plan shows the repair →
execute: fresh-post pos29 → new id → pos30 re-posts `-r 19098`
`depends_on=<newid>` → both published under `-p` → `reviews.db` saved with
the new ids.

## Error handling

- If the re-post fails again, the entry stays empty in the db (existing
  `_execute` partial-failure behavior) and is re-planned on the next run.
  No regression.
- Because the matcher now guarantees no empty-id entry reaches the `KEEP`
  no-op branch, `publish_one("")` / a post with an empty id can no longer
  occur through this path.

## Non-goals

- No change to how the original post failure is detected or prevented.
- No new flag or `ActionKind`.
- No change to `_execute`'s post/publish/close logic, the summary, or
  `rbt.py`.
- No repair of entries whose recorded id is non-empty but stale on
  ReviewBoard (e.g. discarded on the web UI) — that remains the job of
  `--force`.

## Testing

- **Matcher unit** (`tests/test_matcher.py`):
  - One mid-series entry with empty `review_id` → its action is `UPDATE`;
    the immediately following entry is `KEEP_DEP` with
    `needs_dep_update=True`; entries further down stay `KEEP`.
  - Two consecutive empty-id entries → both `UPDATE`; the first entry with a
    real id after them is `KEEP_DEP`.
- **Integration** (`tests/test_rbt_sync.py` + `rbt_mock`): seed a
  mid-series entry with an empty `review_id`, run `gg rbt-sync -p`, and
  assert:
  - a fresh post (no `-r`) is made for the orphan,
  - the successor is re-posted with `-r <its id>` and `depends_on` set to
    the orphan's new id,
  - publish happens for the repaired entries,
  - no post/publish call is made with an empty id,
  - `reviews.db` is saved with the orphan's new id.
- **Dry-run** (`tests/test_rbt_sync.py`): plan shows `update` for the
  orphan, `keep+dep` for the successor, and `r/(lost)` in the Review column.
- **Regression**: existing all-keep tests and the `--force` empty-id
  recovery test (`TestEmptyReviewIdRecovery`) still pass.
