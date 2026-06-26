# Auto-repair orphaned (empty) review_id in `gg rbt-sync` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a matched commit's stored `review_id` is empty (a previous post failed mid-flight), automatically re-post it as a fresh review, refresh the immediate successor's dependency to the new id, and publish both under `-p` — with the repair visible in the `-d` dry-run plan.

**Architecture:** The entire fix lives in the reconciliation layer (`src/gg/matcher.py`) plus one cosmetic display tweak (`src/gg/sync_plan.py`). No new flag, no new `ActionKind`. A matched entry with an empty `review_id` is reclassified `KEEP`→`UPDATE`; `_execute`'s existing `needs_fresh_post` path turns that into a fresh post. `_mark_dep_updates` is taught that an orphaned entry's id "will change", forcing its successor to `KEEP_DEP` so `_execute`'s existing `depends_on` threading repairs the dependency. `_execute`, the summary, and `rbt.py` are untouched.

**Tech Stack:** Python 3 (stdlib, dataclasses, enum), pytest with the `git_repo`/`rbt_mock` fixtures in `tests/conftest.py`.

## Global Constraints

- The repair is **automatic** — no new flag and no new `ActionKind`.
- The fix is confined to `src/gg/matcher.py` (classification + dependency marking) and `src/gg/sync_plan.py` (display). Do **not** change `_execute`, the summary, `rbt.py`, or the DB schema.
- A matched entry with an empty `review_id` is classified `UPDATE` regardless of diff/subject.
- The immediate successor of an orphaned entry is marked `KEEP_DEP` with `needs_dep_update=True`; this generalizes across runs of consecutive empties.
- The plan table renders an empty old-entry id as `r/(lost)`; a `CREATE` (no `old_entry`) keeps its existing `--`.
- Reuse the existing `needs_fresh_post` and `depends_on` machinery in `_execute`; do not duplicate it.
- Use `from __future__ import annotations` and type hints, matching the existing files.

---

### Task 1: Matcher — reclassify orphaned entries and repair successor deps

**Files:**
- Modify: `src/gg/matcher.py` (module constants; the action-building loop in `reconcile`; `_mark_dep_updates`)
- Test: `tests/test_matcher.py` (new `TestOrphanedReviewId` class)

**Interfaces:**
- Consumes: `ActionKind` (`KEEP`, `UPDATE`, `KEEP_DEP`, `DISCARD`), `SyncAction` (fields `kind`, `old_entry`, `new_commit`, `new_position`, `needs_dep_update`), `NewCommit`, `ReviewEntry`, `strip_prefix`, `reconcile(old, new)` — all already defined.
- Produces: a module-level sentinel `_WILL_CHANGE = object()` (private to `matcher.py`); no public signature changes. After this task, `reconcile` classifies an empty-`review_id` matched entry as `UPDATE` and marks the next non-discard entry `KEEP_DEP`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matcher.py` (the helpers `_entry(pos, rid, subject, diff="")` and `_commit(subject, diff="", rev="")` already exist at the top of the file):

```python
class TestOrphanedReviewId:
    def test_empty_review_id_forces_update(self) -> None:
        """A matched entry with an empty review_id is re-posted (UPDATE),
        and its successor is marked KEEP_DEP."""
        old = [
            _entry(0, "100", "alpha", "h1"),
            _entry(1, "", "beta", "h2"),   # orphaned: post failed mid-flight
            _entry(2, "102", "gamma", "h3"),
        ]
        new = [_commit("alpha", "h1"), _commit("beta", "h2"), _commit("gamma", "h3")]
        actions = reconcile(old, new)
        nd = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert len(nd) == 3
        assert nd[0].kind == ActionKind.KEEP            # untouched
        assert nd[1].kind == ActionKind.UPDATE          # orphan re-posted
        assert nd[1].old_entry.review_id == ""
        assert nd[2].kind == ActionKind.KEEP_DEP        # successor dep refreshed
        assert nd[2].needs_dep_update is True

    def test_consecutive_empty_ids(self) -> None:
        """Two consecutive orphans: both UPDATE; the first real successor KEEP_DEP."""
        old = [
            _entry(0, "100", "alpha", "h1"),
            _entry(1, "", "beta", "h2"),
            _entry(2, "", "gamma", "h3"),
            _entry(3, "103", "delta", "h4"),
        ]
        new = [
            _commit("alpha", "h1"), _commit("beta", "h2"),
            _commit("gamma", "h3"), _commit("delta", "h4"),
        ]
        actions = reconcile(old, new)
        nd = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert nd[0].kind == ActionKind.KEEP
        assert nd[1].kind == ActionKind.UPDATE
        assert nd[2].kind == ActionKind.UPDATE
        assert nd[3].kind == ActionKind.KEEP_DEP
        assert nd[3].needs_dep_update is True

    def test_unchanged_series_unaffected(self) -> None:
        """No empty ids: classification is unchanged (regression guard)."""
        old = [_entry(0, "100", "alpha", "h1"), _entry(1, "101", "beta", "h2")]
        new = [_commit("alpha", "h1"), _commit("beta", "h2")]
        actions = reconcile(old, new)
        nd = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert all(a.kind == ActionKind.KEEP for a in nd)
        assert all(not a.needs_dep_update for a in nd)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_matcher.py::TestOrphanedReviewId -v`
Expected: `test_empty_review_id_forces_update` and `test_consecutive_empty_ids` FAIL — the orphan is classified `KEEP` (not `UPDATE`) and the successor is not `KEEP_DEP`. `test_unchanged_series_unaffected` PASSES already.

- [ ] **Step 3: Add the `_WILL_CHANGE` sentinel constant**

In `src/gg/matcher.py`, after the existing weight constants (`_POSITION_WEIGHT = 0.3`), add:

```python
# Sentinel predecessor for _mark_dep_updates. An orphaned entry (empty
# review_id) is re-posted to a brand-new id, so its successor's predecessor
# is guaranteed to differ from any stored value. Comparing this object to a
# real review id (or None) is always unequal, which forces the successor's
# dependency to be refreshed.
_WILL_CHANGE = object()
```

- [ ] **Step 4: Reclassify empty-id matched entries as UPDATE**

In `src/gg/matcher.py`, in the action-building loop of `reconcile`, replace the classification chain:

```python
            entry = old[matches[ni]]
            if commit.diff_hash != entry.diff_hash:
                kind = ActionKind.UPDATE
            elif strip_prefix(commit.subject) != strip_prefix(entry.subject):
                # Diff unchanged but commit subject edited -- push the new summary.
                kind = ActionKind.UPDATE
            else:
                kind = ActionKind.KEEP
```

with:

```python
            entry = old[matches[ni]]
            if not entry.review_id:
                # Orphaned entry: a previous post failed mid-flight, leaving an
                # empty review id. Force a re-post so it gets a real id; the
                # empty-id recovery path in _execute makes it a fresh post.
                kind = ActionKind.UPDATE
            elif commit.diff_hash != entry.diff_hash:
                kind = ActionKind.UPDATE
            elif strip_prefix(commit.subject) != strip_prefix(entry.subject):
                # Diff unchanged but commit subject edited -- push the new summary.
                kind = ActionKind.UPDATE
            else:
                kind = ActionKind.KEEP
```

- [ ] **Step 5: Force the successor's dependency refresh in `_mark_dep_updates`**

In `src/gg/matcher.py`, in `_mark_dep_updates`, change the `prev_review_id` declaration and the trailing per-iteration assignment.

Change the declaration:

```python
    prev_review_id: str | None = None
```

to:

```python
    prev_review_id: object | None = None
```

Then replace the trailing assignment at the end of the loop body:

```python
        prev_review_id = (
            action.old_entry.review_id if action.old_entry else None
        )
```

with:

```python
        if action.old_entry is not None and not action.old_entry.review_id:
            # Orphaned entry will be re-posted to a new id; force its
            # successor to refresh its dependency.
            prev_review_id = _WILL_CHANGE
        elif action.old_entry is not None:
            prev_review_id = action.old_entry.review_id
        else:
            prev_review_id = None
```

(The `expected_pred = old_pred.get(action.old_entry.review_id)` lookup stays as-is: an orphan's own row is now `UPDATE`, so the `if ... kind == ActionKind.KEEP` guard skips it and its colliding `""` key is never read. `CREATE` entries keep their existing `prev_review_id = None` behavior.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_matcher.py -v`
Expected: PASS — the new `TestOrphanedReviewId` tests pass and all pre-existing matcher tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/gg/matcher.py tests/test_matcher.py
git commit -m "fix(matcher): re-post orphaned (empty-id) reviews and fix successor deps"
```

---

### Task 2: Plan table — render a lost review id as `r/(lost)`

**Files:**
- Modify: `src/gg/sync_plan.py` (import; new helper; two render sites in `format_plan`)
- Test: `tests/test_sync_plan.py` (new file)

**Interfaces:**
- Consumes: `format_plan(actions, *, renumber=False, publish=False, reviewers=None, groups=None, force=False)`; `SyncAction`, `ActionKind`, `NewCommit`, `ReviewEntry`.
- Produces: `_review_cell(entry: ReviewEntry | None) -> str` (private helper in `sync_plan.py`). No change to `format_plan`'s signature.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_plan.py`:

```python
"""Tests for gg.sync_plan -- plan table formatting."""

from gg.matcher import ActionKind, NewCommit, SyncAction
from gg.review_store import ReviewEntry
from gg.sync_plan import format_plan


def _entry(pos: int, rid: str, subject: str) -> ReviewEntry:
    return ReviewEntry("feature", pos, rid, subject, f"hash_{pos}")


def _commit(subject: str) -> NewCommit:
    return NewCommit(rev=subject[:8], subject=subject, diff_hash="h")


def test_lost_review_id_rendered_as_lost() -> None:
    """An orphaned (empty-id) entry shows r/(lost); a normal one shows r/<id>."""
    actions = [
        SyncAction(
            kind=ActionKind.UPDATE,
            old_entry=_entry(1, "", "orphan commit"),
            new_commit=_commit("orphan commit"),
            new_position=1,
        ),
        SyncAction(
            kind=ActionKind.KEEP,
            old_entry=_entry(2, "19098", "next commit"),
            new_commit=_commit("next commit"),
            new_position=2,
        ),
    ]
    out = format_plan(actions)
    assert "r/(lost)" in out
    assert "r/19098" in out
    orphan_line = next(l for l in out.splitlines() if "orphan commit" in l)
    assert "r/(lost)" in orphan_line


def test_create_still_shows_dash() -> None:
    """A CREATE (no old_entry) keeps the existing -- in the Review column."""
    actions = [
        SyncAction(
            kind=ActionKind.CREATE,
            old_entry=None,
            new_commit=_commit("brand new"),
            new_position=1,
        ),
    ]
    out = format_plan(actions)
    new_line = next(l for l in out.splitlines() if "brand new" in l)
    assert "--" in new_line
    assert "r/(lost)" not in new_line
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_sync_plan.py -v`
Expected: `test_lost_review_id_rendered_as_lost` FAILS — the empty-id entry currently renders a bare `r/` (no `(lost)`), so `"r/(lost)" in out` is false. `test_create_still_shows_dash` PASSES already.

- [ ] **Step 3: Add the `ReviewEntry` import and `_review_cell` helper**

In `src/gg/sync_plan.py`, add the import alongside the existing matcher import:

```python
from gg.matcher import ActionKind, SyncAction
from gg.numbering import assign_numbers
from gg.review_store import ReviewEntry
```

Then add the helper near the top of the module (after the imports, before `_will_post`):

```python
def _review_cell(entry: ReviewEntry | None) -> str:
    """Review-column text: r/<id>, r/(lost) for an orphaned (empty-id) entry,
    or -- when there is no associated review."""
    if entry is None:
        return "--"
    return f"r/{entry.review_id}" if entry.review_id else "r/(lost)"
```

- [ ] **Step 4: Use the helper at both render sites**

In `src/gg/sync_plan.py`, inside the `for action, num_str in numbered:` loop, replace the DISCARD/SKIP branch's review assignment:

```python
                review = f"r/{action.old_entry.review_id}" if action.old_entry else "--"
                subject = action.old_entry.subject if action.old_entry else ""
```

with:

```python
                review = _review_cell(action.old_entry)
                subject = action.old_entry.subject if action.old_entry else ""
```

and replace the main (non-discard) branch's review assignment:

```python
            review = f"r/{action.old_entry.review_id}" if action.old_entry else "--"
            subject = action.new_commit.subject if action.new_commit else ""
```

with:

```python
            review = _review_cell(action.old_entry)
            subject = action.new_commit.subject if action.new_commit else ""
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_sync_plan.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add src/gg/sync_plan.py tests/test_sync_plan.py
git commit -m "fix(sync-plan): show r/(lost) for orphaned (empty-id) reviews"
```

---

### Task 3: End-to-end integration test through `_execute`

Verifies that Tasks 1–2 wire through real execution: the orphan is re-posted fresh, the successor's dependency is set to the new id, both publish, and no call uses an empty id. Test-only — it relies on the existing `_execute` recovery machinery, so it passes once Tasks 1–2 are done (and would have failed on pre-Task-1 code, where the orphan was a `KEEP` no-op that called `publish_one("")`).

**Files:**
- Test: `tests/test_rbt_sync.py` (new `TestOrphanedReviewIdRepair` class)

**Interfaces:**
- Consumes: the `git_repo`/`rbt_mock` fixtures; module helpers `_post_series(git_repo)`, `_plain(text)`, and the `re` import already at the top of `tests/test_rbt_sync.py`; `review_store.load_reviews(branch, cwd=...)` / `review_store.save_reviews(entries, cwd=...)` / `review_store.ReviewEntry`. Post calls use `-r <id>`, `--depends-on=<id>`, `--target-people <user>`; review ids from `rbt_mock` are `1000 + <prior rbt invocation count>`.
- Produces: nothing (test-only).

- [ ] **Step 1: Write the test**

Append to `tests/test_rbt_sync.py`:

```python
class TestOrphanedReviewIdRepair:
    """A mid-series entry whose stored review_id is empty (its original post
    failed) is auto-re-posted as a fresh review, its successor's dependency is
    refreshed to the new id, and both are published under -p."""

    def test_orphan_repost_fixes_dep_and_publishes(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")    # will be orphaned
        git_repo.commit("gamma")   # successor, keeps its id
        _post_series(git_repo)     # post 3 drafts

        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(entries) == 3
        beta_old_id = entries[1].review_id
        gamma_id = entries[2].review_id
        assert beta_old_id and gamma_id

        # Corrupt beta: simulate its post having failed (empty review_id),
        # preserving subject + diff_hash so reconcile still matches it as KEEP.
        entries[1] = review_store.ReviewEntry(
            branch="feature", position=2, review_id="",
            subject=entries[1].subject, diff_hash=entries[1].diff_hash,
            published=entries[1].published,
        )
        review_store.save_reviews(entries, cwd=git_repo.work_dir)

        initial = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "-p", "-U", "reviewer")
        assert r.returncode == 0, f"stderr: {r.stderr}"

        new_calls = rbt_mock.calls()[initial:]
        post_calls = [c for c in new_calls if c and c[0] == "post"]

        # beta is re-created (fresh post, no -r); gamma is dep-updated (re-post -r)
        fresh_posts = [c for c in post_calls if "-r" not in c]
        repost_calls = [c for c in post_calls if "-r" in c]
        assert len(fresh_posts) == 1, post_calls
        assert len(repost_calls) == 1, post_calls

        # gamma re-posted against its own id
        gc = repost_calls[0]
        assert gc[gc.index("-r") + 1] == gamma_id

        # beta now has a new, non-empty id in the DB
        after = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        beta_new_id = after[1].review_id
        assert beta_new_id and beta_new_id != beta_old_id

        # gamma depends on beta's NEW id
        dep_args = [a for a in gc if a.startswith("--depends-on=")]
        assert dep_args, gc
        assert dep_args[0].split("=", 1)[1] == beta_new_id

        # No post used an empty -r id, and no publish used an empty id
        for c in post_calls:
            if "-r" in c:
                assert c[c.index("-r") + 1] != ""
        for c in new_calls:
            if c and c[0] == "publish":
                assert len(c) >= 2 and c[1] != "", c

    def test_orphan_dry_run_plan_shows_repair(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """-d plan classifies the orphan as update, its successor keep+dep,
        and renders the lost id as r/(lost)."""
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")
        git_repo.commit("gamma")
        _post_series(git_repo)

        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        entries[1] = review_store.ReviewEntry(
            branch="feature", position=2, review_id="",
            subject=entries[1].subject, diff_hash=entries[1].diff_hash,
            published=entries[1].published,
        )
        review_store.save_reviews(entries, cwd=git_repo.work_dir)

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = _plain(r.stdout)
        assert "r/(lost)" in out
        beta_line = next(l for l in out.splitlines() if "beta" in l)
        assert "update" in beta_line
        gamma_line = next(l for l in out.splitlines() if "gamma" in l)
        assert "keep+dep" in gamma_line
```

- [ ] **Step 2: Run the integration tests**

Run: `uv run pytest tests/test_rbt_sync.py::TestOrphanedReviewIdRepair -v`
Expected: PASS (both tests). They exercise the Task 1 + Task 2 changes end-to-end through `_execute`'s existing recovery path.

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_rbt_sync.py
git commit -m "test(rbt-sync): cover orphaned review_id auto-repair end-to-end"
```

---

## Self-Review

**Spec coverage:**
- Force re-post of empty-id entry (spec Component 1) → Task 1 Step 4; tested Task 1 Step 1 + Task 3. ✓
- Successor dependency repair via sentinel threading (spec Component 2) → Task 1 Steps 3, 5; tested Task 1 + Task 3. ✓
- Consecutive-empties generalization → Task 1 `test_consecutive_empty_ids`. ✓
- Plan table `r/(lost)`, CREATE stays `--` (spec Component 3) → Task 2; tested Task 2 + Task 3 dry-run. ✓
- Reuse existing `needs_fresh_post` / `depends_on` (no `_execute` change) → Task 1 relies on it; verified by Task 3 integration. ✓
- Automatic, no new flag/ActionKind → no parser or enum changes in any task. ✓
- Error handling / no empty-id publish → Task 3 asserts no empty `-r` or empty publish id. ✓
- Non-goals (no `_execute`/summary/`rbt.py`/schema change) → no task touches them. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code; every run step has an expected result. ✓

**Type consistency:** `_WILL_CHANGE` (Task 1 Step 3) is referenced only in Task 1 Step 5; `prev_review_id` annotation widened to `object | None` to admit the sentinel. `_review_cell(entry: ReviewEntry | None) -> str` defined and used in Task 2 (Steps 3–4). `ReviewEntry`/`SyncAction`/`ActionKind`/`NewCommit` names match the existing modules. Post-call arg forms (`-r`, `--depends-on=`, `--target-people`) match `rbt_post.py`. ✓

**Deviation note:** The spec's Component 2 mentions "incidentally fixes a latent `old_pred[""]` key-collision." The minimal implementation keeps the `review_id`-keyed `old_pred` because the sentinel makes an orphan's own `expected_pred` lookup irrelevant (orphans are `UPDATE`, skipped by the `KEEP` guard), so the collision is rendered harmless rather than removed. The spec's functional requirement (successor → `KEEP_DEP`) is fully met. This is a simplification, not a behavioral change.
