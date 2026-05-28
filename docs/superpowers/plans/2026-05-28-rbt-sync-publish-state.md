# Publish-state-aware KEEP publishing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track a per-review `published` flag in `reviews.db` so `gg rbt-sync -p` publishes only unpublished drafts (and the plan's Pub column shows it), while already-published reviews are skipped; plus a soft-handle so `rbt publish` on an already-published review is a no-op rather than an error.

**Architecture:** Add a `published` column to the `reviews` table and a `published: bool` field to `ReviewEntry` (default `False`). Every writer of `ReviewEntry` sets it; the KEEP branch of `sync._execute` publishes only when the entry is an unpublished draft; `sync_plan` reflects this in the Pub column. A regex soft-handle in `rbt_publish.publish_one` treats RB "already published / API 100" as success.

**Tech Stack:** Python 3.13, SQLite (`sqlite3`), existing `gg.*` modules, pytest.

**Spec:** `docs/superpowers/specs/2026-05-28-rbt-sync-publish-state-design.md`.

---

## File Structure

- **Modify**
  - `src/gg/review_store.py` — `ReviewEntry.published`, schema, migration, load/save
  - `src/gg/sync.py` — KEEP publishes drafts only; set `published` on all `ReviewEntry` writes
  - `src/gg/sync_plan.py` — Pub column reflects KEEP-draft publishing
  - `src/gg/rbt.py` — set `published` on its `ReviewEntry` writes
  - `src/gg/publish.py` — mark rows `published=1` after a successful publish
  - `src/gg/rbt_import.py` — imported entries are `published=1`
  - `src/gg/rbt_publish.py` — soft-handle API 100 ("already published")
  - `tests/test_review_store.py`, `tests/test_rbt_sync.py`, `tests/test_gorbt.py`, `tests/test_gopublish.py`, `tests/test_rbt_import.py`
  - `README.md`

The reconcile matcher (`gg.matcher`) is untouched: `action.old_entry` is a `ReviewEntry`, so `.published` is already available wherever actions are processed.

---

## Task 1: `review_store` — field, schema, migration, CRUD

**Files:**
- Modify: `src/gg/review_store.py:21-29` (dataclass), `:36-56` (`_connect`), `:59-88` (load/save)
- Test: `tests/test_review_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_store.py` (import `sqlite3` at top if not already imported):

```python
class TestPublishedFlag:
    def test_save_load_round_trips_published(self, git_repo) -> None:
        from gg import review_store
        review_store.save_reviews(
            [
                review_store.ReviewEntry(
                    branch="feature", position=1, review_id="1000",
                    subject="first", diff_hash="a" * 40, published=True,
                ),
                review_store.ReviewEntry(
                    branch="feature", position=2, review_id="1001",
                    subject="second", diff_hash="b" * 40, published=False,
                ),
            ],
            cwd=git_repo.work_dir,
        )
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert [e.published for e in entries] == [True, False]

    def test_default_published_is_false(self, git_repo) -> None:
        from gg import review_store
        e = review_store.ReviewEntry(
            branch="feature", position=1, review_id="1000",
            subject="x", diff_hash="a" * 40,
        )
        assert e.published is False

    def test_migration_backfills_existing_rows_as_published(self, git_repo) -> None:
        import sqlite3
        from gg import review_store
        db = git_repo.work_dir / ".gg" / "reviews.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        # Pre-feature schema: no `published` column.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE reviews ("
            "branch TEXT NOT NULL, position INTEGER NOT NULL, "
            "review_id TEXT NOT NULL, subject TEXT NOT NULL, "
            "diff_hash TEXT NOT NULL, PRIMARY KEY (branch, position))"
        )
        conn.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
            ("feature", 1, "1000", "legacy", "a" * 40),
        )
        conn.commit()
        conn.close()
        # load_reviews → _connect → migration backfills published=1
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(entries) == 1
        assert entries[0].published is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review_store.py::TestPublishedFlag -v`
Expected: FAIL — `ReviewEntry.__init__() got an unexpected keyword argument 'published'`.

- [ ] **Step 3: Add the field**

In `src/gg/review_store.py`, change the dataclass (lines 21-29):

```python
@dataclass
class ReviewEntry:
    """A single review in a posted series."""

    branch: str
    position: int
    review_id: str
    subject: str
    diff_hash: str
    published: bool = False
```

- [ ] **Step 4: Add the column and migration in `_connect`**

In `src/gg/review_store.py`, replace the `_connect` body (lines 36-56) with:

```python
def _connect(*, cwd: Path | None = None) -> sqlite3.Connection:
    path = _db_path(cwd=cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reviews (
            branch TEXT NOT NULL,
            position INTEGER NOT NULL,
            review_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            diff_hash TEXT NOT NULL,
            published INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (branch, position)
        );
        CREATE TABLE IF NOT EXISTS diff_hashes (
            branch TEXT NOT NULL,
            diff_hash TEXT NOT NULL,
            PRIMARY KEY (branch, diff_hash)
        );
    """)
    # Migrate a pre-feature reviews table (no `published` column). Legacy
    # rows predate this feature and are assumed already published.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()]
    if "published" not in cols:
        conn.execute(
            "ALTER TABLE reviews ADD COLUMN published INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()
    return conn
```

- [ ] **Step 5: Update load/save to carry `published`**

In `src/gg/review_store.py`, replace `load_reviews` (lines 59-70):

```python
def load_reviews(branch: str, *, cwd: Path | None = None) -> list[ReviewEntry]:
    """Load all reviews for a branch, ordered by position."""
    conn = _connect(cwd=cwd)
    try:
        rows = conn.execute(
            "SELECT branch, position, review_id, subject, diff_hash, published "
            "FROM reviews WHERE branch = ? ORDER BY position",
            (branch,),
        ).fetchall()
        return [
            ReviewEntry(
                branch=r[0], position=r[1], review_id=r[2],
                subject=r[3], diff_hash=r[4], published=bool(r[5]),
            )
            for r in rows
        ]
    finally:
        conn.close()
```

And replace the `executemany` in `save_reviews` (lines 81-85):

```python
        conn.execute("DELETE FROM reviews WHERE branch = ?", (branch,))
        conn.executemany(
            "INSERT INTO reviews "
            "(branch, position, review_id, subject, diff_hash, published) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.branch, e.position, e.review_id, e.subject,
                 e.diff_hash, int(e.published))
                for e in entries
            ],
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_review_store.py -v`
Expected: PASS (new tests + existing ones).

- [ ] **Step 7: Run the full suite (regression check)**

Run: `uv run pytest tests/ 2>&1 | tail -3`
Expected: all pass. (Existing `ReviewEntry(...)` call sites omit `published`, defaulting to `False`; nothing reads it yet.)

- [ ] **Step 8: Commit**

```bash
git add src/gg/review_store.py tests/test_review_store.py
git commit -m "$(cat <<'EOF'
feat(review_store): add published flag to reviews with migration

Adds a `published` column (and ReviewEntry.published) plus a connect-
time migration that backfills pre-feature rows as published=1. No
behavior change yet; the flag is not read until sync wires it in.
EOF
)"
```

---

## Task 2: `sync._execute` — publish only unpublished KEEP drafts

**Files:**
- Modify: `src/gg/sync.py:121-141` (KEEP branch), `:192-198` (post-write), `:348-357` (SKIP-preserve)
- Test: `tests/test_rbt_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rbt_sync.py` inside `class TestPublishUnchangedOnSync` (after `test_no_publish_when_flag_absent`):

```python
    def test_keep_publish_skips_already_published(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """First -p publishes the drafts; a second -p makes no publish call."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)  # gg rbt without -p → drafts (published=0)

        n0 = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "-p")
        assert r.returncode == 0
        pub1 = [c for c in rbt_mock.calls()[n0:] if c and c[0] == "publish"]
        assert len(pub1) == 2  # both drafts published

        n1 = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "-p")
        assert r.returncode == 0
        pub2 = [c for c in rbt_mock.calls()[n1:] if c and c[0] == "publish"]
        assert pub2 == []  # already published → no re-publish
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rbt_sync.py::TestPublishUnchangedOnSync::test_keep_publish_skips_already_published -v`
Expected: FAIL — the second run currently publishes again (`pub2` has 2 entries) because KEEP always publishes.

- [ ] **Step 3: Change the KEEP branch**

In `src/gg/sync.py`, replace the KEEP block (lines 121-141):

```python
        if (action.kind == ActionKind.KEEP
                and not action.needs_dep_update
                and (not renumber
                     or _number_matches(action.old_entry, num_str, old_total))):
            # Nothing about the commit changed. With --publish, publish the
            # review only if it is still an unpublished draft; an already
            # published review is left untouched.
            assert action.old_entry is not None
            entry_published = bool(action.old_entry.published)
            if publish and not entry_published:
                rc = publish_one(
                    action.old_entry.review_id,
                    dry_run=dry_run, verbose=verbose, cwd=cwd,
                )
                if rc == 0:
                    entry_published = True
            entries.append(review_store.ReviewEntry(
                branch=branch_name,
                position=len(entries) + 1,
                review_id=action.old_entry.review_id,
                subject=review_store.strip_prefix(action.new_commit.subject),
                diff_hash=action.new_commit.diff_hash,
                published=entry_published,
            ))
            prev_review_id = action.old_entry.review_id
            continue
```

- [ ] **Step 4: Set `published` on the post-write**

In `src/gg/sync.py`, replace the entry append after posting (lines 192-198):

```python
        entries.append(review_store.ReviewEntry(
            branch=branch_name,
            position=len(entries) + 1,
            review_id=rid or "",
            subject=review_store.strip_prefix(action.new_commit.subject),
            diff_hash=action.new_commit.diff_hash,
            published=bool(publish),
        ))
```

- [ ] **Step 5: Carry `published` on the SKIP-preserve write**

In `src/gg/sync.py`, replace the SKIP-preservation append (lines 348-357):

```python
    # Preserve skipped-discard entries so they reappear next sync
    for a in actions:
        if a.kind == ActionKind.SKIP and a.old_entry and not a.new_commit:
            entries.append(review_store.ReviewEntry(
                branch=branch_name,
                position=len(entries) + 1,
                review_id=a.old_entry.review_id,
                subject=a.old_entry.subject,
                diff_hash=a.old_entry.diff_hash,
                published=bool(a.old_entry.published),
            ))
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestPublishUnchangedOnSync -v`
Expected: PASS (new test + the existing `test_publish_publishes_kept_reviews`, which exercises only the first-run publish of drafts).

- [ ] **Step 7: Run the full sync test file (regression)**

Run: `uv run pytest tests/test_rbt_sync.py -v 2>&1 | tail -8`
Expected: all pass, including `TestSyncRetry` (those run a single `-p` over fresh drafts, which still publish).

- [ ] **Step 8: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "$(cat <<'EOF'
feat(rbt-sync): publish only unpublished KEEP drafts under -p

KEEP now publishes a review only when its recorded `published` flag is
false, and records published=1 after a successful publish. Already-
published reviews are left untouched, so a repeat `rbt-sync -p` makes
no publish call. Posting and SKIP-preserve writes set `published` too.
EOF
)"
```

---

## Task 3: `sync_plan` — Pub column reflects KEEP-draft publishing

**Files:**
- Modify: `src/gg/sync_plan.py:11-20` (helpers), `:52` (`show_pub`)
- Test: `tests/test_rbt_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rbt_sync.py` inside `class TestSyncDryRun`:

```python
    def test_plan_pub_column_tracks_published_state(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        import re as _re
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)  # draft, published=0

        # Unpublished draft under -p → plan shows "keep ... yes"
        r = git_repo.run_gg("rbt-sync", "-d", "-p")
        assert r.returncode == 0
        assert _re.search(r"keep\s+yes", _plain(r.stdout)), _plain(r.stdout)

        # Publish it, then the plan shows "keep ... --"
        git_repo.run_gg("rbt-sync", "-p")
        r = git_repo.run_gg("rbt-sync", "-d", "-p")
        assert r.returncode == 0
        assert _re.search(r"keep\s+--", _plain(r.stdout)), _plain(r.stdout)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncDryRun::test_plan_pub_column_tracks_published_state -v`
Expected: FAIL on the first assertion — KEEP currently always renders `--` in the Pub column.

- [ ] **Step 3: Add `_will_publish_keep` and update `_pub_label`**

In `src/gg/sync_plan.py`, replace lines 11-20:

```python
def _will_post(action: SyncAction) -> bool:
    """True if this action will post to ReviewBoard."""
    return action.kind in (ActionKind.UPDATE, ActionKind.CREATE) or action.needs_dep_update


def _will_publish_keep(action: SyncAction, publish: bool) -> bool:
    """True if a KEEP review will be published (it is an unpublished draft)."""
    return (
        publish
        and action.kind == ActionKind.KEEP
        and action.old_entry is not None
        and not action.old_entry.published
    )


def _pub_label(action: SyncAction, publish: bool) -> str:
    """Pub column value for an action."""
    if _will_post(action):
        return "yes" if publish else "draft"
    if _will_publish_keep(action, publish):
        return "yes"
    return "--"
```

- [ ] **Step 4: Update `show_pub`**

In `src/gg/sync_plan.py`, replace line 52:

```python
    show_pub = any(_will_post(a) or _will_publish_keep(a, publish) for a in actions)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncDryRun::test_plan_pub_column_tracks_published_state -v`
Expected: PASS.

- [ ] **Step 6: Run the full sync test file (regression)**

Run: `uv run pytest tests/test_rbt_sync.py -v 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/gg/sync_plan.py tests/test_rbt_sync.py
git commit -m "feat(rbt-sync): plan Pub column shows yes for unpublished KEEP drafts"
```

---

## Task 4: `gg rbt` records `published`

**Files:**
- Modify: `src/gg/rbt.py:124-129` (single-commit), `:205-210` (multi-commit)
- Test: `tests/test_gorbt.py`

(Line numbers are approximate; both sites are `review_entries.append(review_store.ReviewEntry(...))`.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gorbt.py` (top-level or inside an existing class — place it at module level after the existing classes):

```python
class TestRbtPublishedFlag:
    def test_post_without_publish_records_draft(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("BUG-1: fix crash")
        git_repo.run_gg("rbt")  # no -p
        e = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(e) == 1
        assert e[0].published is False

    def test_post_with_publish_records_published(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("BUG-1: fix crash")
        git_repo.run_gg("rbt", "-p")
        e = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(e) == 1
        assert e[0].published is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gorbt.py::TestRbtPublishedFlag -v`
Expected: `test_post_with_publish_records_published` FAILS (records `False` from the default); the draft test passes incidentally.

- [ ] **Step 3: Set `published` at both append sites**

In `src/gg/rbt.py`, the single-commit append (around line 124):

```python
            if result.review_id:
                review_entries.append(review_store.ReviewEntry(
                    branch=branch_name, position=1,
                    review_id=result.review_id,
                    subject=review_store.strip_prefix(summary_text),
                    diff_hash=h,
                    published=bool(args.publish),
                ))
```

And the multi-commit append (around line 205):

```python
        if result.review_id:
            depends = result.review_id
            review_entries.append(review_store.ReviewEntry(
                branch=branch_name, position=idx,
                review_id=result.review_id,
                subject=review_store.strip_prefix(summary_text),
                diff_hash=h,
                published=bool(args.publish),
            ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gorbt.py::TestRbtPublishedFlag -v`
Expected: PASS.

- [ ] **Step 5: Run the full gorbt test file (regression)**

Run: `uv run pytest tests/test_gorbt.py -v 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/gg/rbt.py tests/test_gorbt.py
git commit -m "feat(rbt): record published flag from --publish on posted entries"
```

---

## Task 5: `gg publish` marks rows `published=1`

**Files:**
- Modify: `src/gg/publish.py:43-53` (the non-dry publish loop)
- Test: `tests/test_gopublish.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gopublish.py`:

```python
class TestPublishMarksState:
    def test_publish_marks_entries_published(
        self, git_repo, rbt_mock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        # Seed drafts via gg rbt (no -p)
        r = git_repo.run_gg("rbt")
        assert r.returncode == 0
        assert all(
            not e.published
            for e in review_store.load_reviews("feature", cwd=git_repo.work_dir)
        )

        r = git_repo.run_gg("publish")
        assert r.returncode == 0
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert entries and all(e.published for e in entries)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gopublish.py::TestPublishMarksState -v`
Expected: FAIL — after `gg publish`, rows are still `published=False` (publish.py never updates them).

- [ ] **Step 3: Update the publish loop**

In `src/gg/publish.py`, add `from dataclasses import replace` to the imports, then replace the non-dry loop (lines 43-53):

```python
    failures = 0
    updated: list[review_store.ReviewEntry] = []
    for e in entries:
        rc = publish_one(e.review_id, dry_run=False, verbose=args.verbose, cwd=cwd)
        if rc == 0:
            updated.append(replace(e, published=True))
        else:
            failures += 1
            updated.append(e)

    review_store.save_reviews(updated, cwd=cwd)

    if failures:
        print(f"[gg] {failures} of {len(entries)} publish call(s) failed.", file=sys.stderr)
        return 1

    print(f"Published {len(entries)} review(s).", file=sys.stderr)
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_gopublish.py::TestPublishMarksState -v`
Expected: PASS.

- [ ] **Step 5: Run the full gopublish test file (regression)**

Run: `uv run pytest tests/test_gopublish.py -v 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/gg/publish.py tests/test_gopublish.py
git commit -m "feat(publish): mark reviews published=1 after a successful publish"
```

---

## Task 6: `gg rbt-import` records `published=1`

**Files:**
- Modify: `src/gg/rbt_import.py:125-131`
- Test: `tests/test_rbt_import.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rbt_import.py` (match the file's existing fixture usage; imports of `review_store` may already exist):

```python
def test_import_marks_entries_published(git_repo, rbt_mock) -> None:
    """Imported reviews are established on RB, so they are recorded published."""
    from gg import review_store
    git_repo.create_branch("feature", "master")
    git_repo.commit("fix crash")
    # 1000 is the first id the rbt_mock hands out; import the single-review chain.
    r = git_repo.run_gg("rbt-import", "1000")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
    assert entries and all(e.published for e in entries)
```

> Note for the implementer: confirm how existing `tests/test_rbt_import.py` drives a successful import (the `rbt_mock` `api-get` path and how a chain terminates). If the single-id form above does not produce a clean one-entry import in this mock, mirror the setup of the existing happy-path import test in that file and assert `all(e.published for e in entries)` on its result.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rbt_import.py::test_import_marks_entries_published -v`
Expected: FAIL — imported entries default to `published=False`.

- [ ] **Step 3: Set `published=True` on import**

In `src/gg/rbt_import.py`, replace the append (lines 125-131):

```python
        entries.append(review_store.ReviewEntry(
            branch=branch_name,
            position=idx,
            review_id=review_id,
            subject=subject,
            diff_hash=h,
            published=True,
        ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rbt_import.py::test_import_marks_entries_published -v`
Expected: PASS.

- [ ] **Step 5: Run the full import test file (regression)**

Run: `uv run pytest tests/test_rbt_import.py -v 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/gg/rbt_import.py tests/test_rbt_import.py
git commit -m "feat(rbt-import): record imported reviews as published"
```

---

## Task 7: `publish_one` soft-handles "already published"

**Files:**
- Modify: `src/gg/rbt_publish.py:1-35`
- Test: `tests/test_gopublish.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gopublish.py`:

```python
class TestPublishAlreadyPublished:
    def test_api_100_is_soft_no_op(self, git_repo, rbt_mock) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt")  # seed one draft
        assert r.returncode == 0

        rbt_mock.queue_failure(
            output=(
                "ERROR: Error publishing review request (it may already be "
                "published): Object does not exist (API Error 100: Does Not "
                "Exist)\n"
            ),
            returncode=1,
            count=1,
        )
        r = git_repo.run_gg("publish")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "already published" in r.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gopublish.py::TestPublishAlreadyPublished -v`
Expected: FAIL — `publish_one` returns the failing exit code, so `gg publish` exits 1.

- [ ] **Step 3: Add the soft-handle**

Replace `src/gg/rbt_publish.py` entirely with:

```python
"""Publish ReviewBoard review-request drafts via `rbt publish`."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from gg.rbt_retry import run_with_retry

# RB reports a publish against a request with no pending draft as
# API Error 100 ("Does Not Exist"), wrapped as "may already be published".
_ALREADY_PUBLISHED_RE = re.compile(
    r"API Error 100|may already be published",
    re.IGNORECASE,
)


def publish_one(
    review_id: str,
    *,
    dry_run: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
) -> int:
    """Publish a single review request draft. Returns rbt's exit code.

    Treats RB's "already published / no draft" (API Error 100) as a
    successful no-op so re-running publish over public reviews is benign.
    """
    cmd = ["rbt", "publish", review_id]

    if dry_run:
        print(shlex.join(cmd))
        return 0

    r = run_with_retry(cmd, cwd=cwd)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        if _ALREADY_PUBLISHED_RE.search(out):
            sys.stderr.write(
                f"[gg] r/{review_id} already published (nothing to publish)\n"
            )
            return 0
        sys.stderr.write(f"\n[gg] rbt publish r/{review_id} failed (exit {r.returncode})\n")
        sys.stderr.write(out)
        return r.returncode
    if verbose:
        out = r.stdout + r.stderr
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_gopublish.py::TestPublishAlreadyPublished -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite (regression)**

Run: `uv run pytest tests/ 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/gg/rbt_publish.py tests/test_gopublish.py
git commit -m "$(cat <<'EOF'
fix(publish): treat "already published" (API 100) as a no-op

rbt publish against a review with no pending draft errors with API
Error 100. Detect it and return success with an info line instead of
surfacing a failure, so re-publishing public reviews is benign.
EOF
)"
```

---

## Task 8: README

**Files:**
- Modify: `README.md` (the "Post, review, publish" subsection, ~lines 200-216)

- [ ] **Step 1: Rewrite the publish note**

In `README.md`, find the paragraph after the "Post, review, publish" code block:

```markdown
`gg rbt -u --publish` and `gg rbt-sync -p` also publish unchanged
drafts (in addition to creating/updating reviews whose diff changed).
```

Replace it with:

```markdown
`gg rbt-sync -p` publishes the reviews it creates or updates, plus any
unchanged (KEEP) reviews that are still sitting as unpublished drafts.
Reviews that are already published are left untouched -- a repeat
`gg rbt-sync -p` over a published branch makes no publish calls. gg
tracks per-review publish state in `reviews.db`; `gg publish` remains
the way to (re)publish every recorded draft on a branch.
```

- [ ] **Step 2: Verify it reads correctly**

Run: `sed -n '198,220p' README.md` (or a range read)
Expected: the new paragraph is in place and the surrounding flow is intact.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): clarify rbt-sync -p publishes only unpublished drafts"
```

---

## Self-review checklist (applied)

- **Spec coverage:** data model + migration → Task 1; execution (KEEP/CREATE/UPDATE/SKIP writes) → Task 2; plan display → Task 3; cross-command writers → Tasks 4 (rbt), 5 (publish), 6 (import); soft-handle → Task 7; README → Task 8. All spec sections mapped.
- **Placeholders:** none; every code step shows the full code. Task 6's note asks the implementer to mirror the existing import happy-path if the single-id form doesn't drive a clean import in the mock — this is verification guidance, not a deferred decision.
- **Type/name consistency:** `published: bool` field; `_will_publish_keep(action, publish)`; `int(e.published)` on save, `bool(r[5])` on load; `replace(e, published=True)` in publish.py. Consistent across tasks.
