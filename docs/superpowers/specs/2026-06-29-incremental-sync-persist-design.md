# Incremental DB persistence in gg rbt-sync

Date: 2026-06-29
Status: Approved (pending spec review)

## Goal

Persist `.gg/reviews.db` (review entries + diff hashes) after each action during
`gg rbt-sync`, so that interrupting the command (Ctrl-C / crash) leaves the local
DB reflecting all *completed* actions rather than discarding every update.

## Background

`gg rbt-sync` posts/updates/publishes each commit's review to ReviewBoard
incrementally inside `sync._execute`, accumulating `ReviewEntry` objects in
memory, and returns the list. `sync.run` then writes the DB **once at the very
end** (`review_store.save_reviews(entries)` + `diff_cache.save_hashes(...)`).

Because the only DB write is at the end, an interrupt after some reviews were
already posted to ReviewBoard but before that final save records **none** of
them. The next sync then sees those reviews as missing and tries to re-post them.

## Design

### Persist callback

`sync._execute` gains a parameter:

```python
persist: Callable[[list[review_store.ReviewEntry]], None] | None = None
```

Immediately after `_execute` appends each action's `ReviewEntry` to its `entries`
list, it calls `persist(entries)` with the accumulated list so far (when
`persist` is not None). This is the single new behavior inside `_execute`; the
ReviewBoard posting logic is otherwise unchanged.

`sync.run` builds the closure and passes it in. The closure:
- merges the *preserved* entries (skipped-discard rows — SKIP actions with an
  `old_entry` and no `new_commit`, which `run` currently appends after the loop)
  with the supplied accumulated entries, then
- calls `review_store.save_reviews(merged)` and
  `diff_cache.save_hashes({e.diff_hash for e in merged}, cwd=cwd, branch=branch)`.

`run` computes the preserved entries once before invoking `_execute` so every
incremental save includes them.

Keeping persistence in a callback keeps `_execute`'s RB logic decoupled from
storage and makes it directly unit-testable: a recording callback asserts that a
save happened after each action. Dry-run and unit tests that don't want DB writes
pass `persist=None`.

### Atomicity

`review_store.save_reviews` already performs `DELETE FROM reviews WHERE branch=?`
+ `executemany(INSERT ...)` + `conn.commit()` for the branch in one SQLite
transaction, and returns early if given an empty list. So each incremental save
is atomic — an interrupt during a save leaves either the prior committed state or
the new one, never a half-written table. `diff_cache.save_hashes` (via
`save_diff_hashes`) is likewise a transactional replace.

### End-of-run backstop

`run` keeps its existing final save after `_execute` returns. This covers the
cases the in-loop persist cannot:
- **All-skipped run:** no action entries are appended, so the in-loop `persist`
  never fires; the final save persists the preserved entries.
- **Preserved-only / position fix-up:** the post-loop skipped-discard append and
  the authoritative final ordering are written once at the end.

### Residual lag (accepted)

The only remaining unprotected window is an interrupt *during* a single
`post_one` subprocess: that action may have reached ReviewBoard but its entry is
not yet saved. We persist immediately after `post_one` returns, so the exposure
shrinks from "all actions" to "at most the one action in flight" — the best
achievable without transactional coupling to ReviewBoard.

## Edge cases

- `save_reviews([])` returns early (never clears the branch with an empty list),
  so the first incremental call (one entry) is the earliest write.
- Failed `post_one` still appends an entry (empty `review_id`) and continues; that
  entry is persisted like any other — unchanged from today, just saved sooner.
- Positions on a partial save are 1..k; a subsequent sync rebuilds them via
  `reconcile`, so partial positions are harmless.

## Files touched (~/git-helpers)

- `src/gg/sync.py` — add `persist` param to `_execute`; call it after each entry;
  in `run`, compute preserved entries up front and pass a persisting closure;
  keep the final save as backstop.
- `tests/test_rbt_sync.py` — add a test that `_execute` (or `run`) persists after
  each action: a recording/persisting callback observes one save per posted
  action, and the DB after a simulated mid-run stop contains the completed
  entries.

## Non-goals / YAGNI

- No change to the `--close`, `--adopt`, or dry-run paths beyond passing
  `persist=None` where applicable.
- No SIGINT handler / transactional coupling with ReviewBoard.
- No switch from full-replace `save_reviews` to per-row upsert (negligible cost at
  these series sizes; reuse the proven primitive).

## Open questions

None.
