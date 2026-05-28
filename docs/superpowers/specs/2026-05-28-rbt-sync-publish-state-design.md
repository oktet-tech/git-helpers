# Publish-state-aware KEEP publishing for `gg rbt-sync -p`

**Status:** approved
**Date:** 2026-05-28

## Problem

`gg rbt-sync -p` currently publishes *every* KEEP (unchanged) review by
calling `rbt publish <id>` on it, regardless of whether that review is
still an unpublished draft or was already published. Two consequences:

1. **Hard failure on already-published reviews.** `rbt publish` against
   a review with no pending draft errors with
   `API Error 100: Does Not Exist` ("it may already be published"), which
   `publish_one` treats as a failure. A routine `gg rbt-sync -p` over a
   branch whose reviews are already public dumps a wall of errors.
2. **The plan lies.** `sync_plan` shows `Pub --` for KEEP rows (because
   `_will_post` excludes KEEP), but `_execute` publishes them anyway.

## Goal

Publish a KEEP review only when it has unpublished changes (a draft).
Skip already-published reviews silently. Make the plan's `Pub` column
reflect what will actually happen.

Desired behavior, in the user's words: *"If I had a draft and I give
--publish: we should publish and the plan should show publish. If I
already had a review request published and --publish is given, but
nothing has actually changed we should not re-publish it."*

## Non-goals

- Querying ReviewBoard for live draft state. gg keeps local state in
  `reviews.db`; we track publish state there rather than issuing an
  `rbt api-get` per review on every `-p` run.
- Changing how CREATE / UPDATE / KEEP_DEP publish. They already publish
  as part of `rbt post -p` (a single call); only the standalone
  KEEP-publish path changes.
- Auto-publishing drafts created outside gg (e.g. web-UI edits). gg only
  reasons about state it recorded itself; the soft-handle keeps such
  cases from erroring.

## Data model

Add a `published` flag to the reviews table and to `ReviewEntry`.

```sql
CREATE TABLE IF NOT EXISTS reviews (
    branch TEXT NOT NULL,
    position INTEGER NOT NULL,
    review_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    diff_hash TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (branch, position)
);
```

`ReviewEntry` gains `published: bool` (stored as 0/1).

**Migration.** On connect, after the `CREATE TABLE IF NOT EXISTS`, run
`PRAGMA table_info(reviews)`; if `published` is absent,
`ALTER TABLE reviews ADD COLUMN published INTEGER NOT NULL DEFAULT 0`.
Existing rows become `0` (treated as drafts). The first `-p` run after
upgrade attempts to publish them; already-published ones are absorbed by
the soft-handle (below) and marked `1`, so the state self-heals.

**Meaning:** "the latest posted state of this review has been
published."

| Event | `published` |
|---|---|
| CREATE / UPDATE / KEEP_DEP without `-p` | `0` (new unpublished draft) |
| CREATE / UPDATE / KEEP_DEP with `-p` | `1` (posted and published in one `rbt post -p`) |
| KEEP, no publish action | carries over `old_entry.published` |
| KEEP + `-p`, was `0`, publish succeeds | `1` |
| KEEP + `-p`, was `1` | stays `1` (no rbt call) |
| `gg publish` publishes an entry | `1` |
| `gg rbt-import` (existing RB chain) | `1` (established reviews) |
| SKIP-preserved discard entry | carries over `old_entry.published` |

`published` resets to `0` whenever a new draft is created (UPDATE without
`-p`) and becomes `1` only on a successful publish.

## Execution

`_execute` in `src/gg/sync.py`:

- **KEEP branch:** call `publish_one` only when
  `publish and not action.old_entry.published`. On success (rc 0,
  including soft-handled "already published"), record the entry with
  `published=True`. When `old_entry.published` is already truthy, make no
  `rbt publish` call and keep `published=True`.
- **CREATE / UPDATE / KEEP_DEP branches:** posting logic unchanged
  (`post_one(publish=publish)` already publishes in-call); record the
  entry with `published=bool(publish)`.
- **Recovery branch** (empty `review_id`, treated as fresh post): same as
  CREATE — `published=bool(publish)`.
- **SKIP-preservation block:** carry over `a.old_entry.published`.

## Plan display

`src/gg/sync_plan.py`:

- New helper `_will_publish_keep(action, publish)`: returns True iff
  `publish and action.kind == ActionKind.KEEP and action.old_entry is
  not None and not action.old_entry.published`.
- `_pub_label(action, publish)`:
  - `_will_post(action)` → `"yes"` if `publish` else `"draft"` (unchanged)
  - `_will_publish_keep(action, publish)` → `"yes"`
  - otherwise → `"--"`
- `show_pub = any(_will_post(a) or _will_publish_keep(a, publish) for a in
  actions)` so an all-KEEP branch with unpublished drafts under `-p` still
  renders the `Pub` column.

## Cross-command consistency

Every writer of `ReviewEntry` sets `published`:

- `src/gg/rbt.py` (`gg rbt`): created/updated entries use
  `published=bool(args.publish)`. The unchanged-draft publish path marks
  `published=1` after a successful publish. (This seeds `published=0` for
  the "post drafts with `gg rbt`, publish later" flow.)
- `src/gg/publish.py` (`gg publish`): after a successful publish of each
  entry, rewrite its row with `published=1`.
- `src/gg/rbt_import.py`: imported entries use `published=1`.

## Soft-handle

`src/gg/rbt_publish.publish_one`: when `rbt publish` returns non-zero and
the combined output matches an "already published" signal
(`API Error 100` / "may already be published"), print an info line
`[gg] r/<id> already published (nothing to publish)` and return `0`
instead of surfacing the error. Safety net for migration stale rows,
web-UI publishes, and `gg publish` over already-public reviews.

## Testing

1. **`review_store`**: round-trips `published`; migration adds the column
   to a pre-existing DB created without it (existing rows read back as
   `published=0`).
2. **KEEP + `-p`, draft**: entry `published=0` → exactly one `rbt publish`
   call; row afterward has `published=1`.
3. **KEEP + `-p`, already published**: entry `published=1` → zero
   `rbt publish` calls; row stays `published=1`.
4. **Plan display**: unpublished KEEP under `-p` shows `Pub yes`; once
   `published=1` it shows `Pub --`.
5. **`gg rbt`**: without `-p` records `published=0`; with `-p` records
   `published=1`.
6. **`gg publish`**: flips rows to `published=1`.
7. **Soft-handle**: `rbt publish` returning API 100 → `publish_one`
   returns 0 and prints the info line (use `rbt_mock.queue_failure`).
8. **Behavior reversal**: rewrite `TestPublishUnchangedOnSync` and the
   `TestSyncRetry` KEEP-publish tests to the new semantics — a draft KEEP
   publishes once (and the 207-retry path still applies to that first
   publish), and a second `-p` run makes no publish call.

## Documentation

Rewrite the "Post, review, publish" subsection in `README.md`:
`gg rbt-sync -p` publishes reviews it creates/updates plus any KEEP
reviews still sitting as unpublished drafts, and silently skips reviews
already published. `gg publish` remains the way to (re)publish every
recorded draft on a branch.

## Migration / compatibility

- Schema change is additive and self-applied on first connect; no manual
  migration step.
- Behavior change: `gg rbt-sync -p` no longer re-publishes
  already-published KEEP reviews. The previously-documented "publishes
  unchanged drafts" wording is refined — it publishes *unpublished*
  drafts, which is the intended meaning.
