# Incremental DB persistence in gg rbt-sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist `.gg/reviews.db` after each action in `gg rbt-sync` so an interrupt
keeps all completed actions instead of discarding every update.

**Architecture:** `sync._execute` gains a `persist` callback it calls after each
appended entry. `sync.run` passes a closure that merges the preserved
(skipped-discard) rows and calls `save_reviews` + `save_hashes`; the existing
end-of-run save stays as a backstop. Each `save_reviews` is a single SQLite
transaction (atomic).

**Tech Stack:** Python 3.10+, sqlite (via `gg.review_store`/`gg.diff_cache`), pytest.
Run tests: `cd /home/kostik/git-helpers && uv run --quiet pytest <paths> -q`.

All work on branch `ai-fix`.

---

## File structure

- `src/gg/sync.py` — add `persist` param to `_execute` and call it after each entry
  append; add module helper `_preserved_entries`; rewire `run` to build a persisting
  closure and pass it in, keeping a final backstop save.
- `tests/test_rbt_sync.py` — unit-test that `_execute` persists after each action, and
  that `_preserved_entries` returns the skipped-discard rows.

---

## Task 1: `_execute` calls `persist` after each appended entry

**Files:** Modify `src/gg/sync.py`; Test `tests/test_rbt_sync.py`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_rbt_sync.py` (read its
  imports first; it already imports `review_store`; add what this test needs):

```python
def test_execute_persists_after_each_action(monkeypatch, tmp_path):
    from gg import matcher, sync
    from gg.rbt_post import PostResult

    new = [matcher.NewCommit(rev="aaa", subject="first", diff_hash="h1"),
           matcher.NewCommit(rev="bbb", subject="second", diff_hash="h2")]
    actions = matcher.reconcile([], new)

    posts = iter(["100", "101"])
    monkeypatch.setattr(sync, "post_one",
                        lambda *a, **k: PostResult(review_id=next(posts), output=""))
    monkeypatch.setattr(sync.rb_api, "fetch_reviewers", lambda *a, **k: ([], []))

    snapshots: list[list[str]] = []
    result = sync._execute(
        actions,
        branch_name="feature", tracking="origin/master",
        renumber=False, publish=False, verbose=False, progress=False,
        dry_run=False, explicit_branch=None, initial_depends=None,
        reviewers=["rev"], groups=None, no_numbers=False,
        persist=lambda entries: snapshots.append([e.review_id for e in entries]),
        cwd=tmp_path,
    )
    assert [e.review_id for e in result] == ["100", "101"]
    # one persist call per action, with the accumulated list growing each time
    assert snapshots == [["100"], ["100", "101"]]
```

- [ ] **Step 2: Run, expect fail** —
  `cd /home/kostik/git-helpers && uv run --quiet pytest tests/test_rbt_sync.py::test_execute_persists_after_each_action -q`.
  Expected: `TypeError: _execute() got an unexpected keyword argument 'persist'`.

- [ ] **Step 3: Add the `persist` parameter** to `_execute` in `src/gg/sync.py`. The
  signature's keyword-only block currently ends with `no_numbers: bool = False,` then
  `cwd: Path,`. Insert `persist` between them, and add the `Callable` import. At the top
  of the file ensure `from collections.abc import Callable` is imported (add it if
  missing). Change the signature to include:

```python
    no_numbers: bool = False,
    persist: Callable[[list[review_store.ReviewEntry]], None] | None = None,
    cwd: Path,
```

- [ ] **Step 4: Call `persist` after each `entries.append(...)`** in `_execute`. There
  are exactly two append sites:

  (a) the unchanged/keep branch, currently:
```python
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
  Insert a persist call right after the `entries.append(...)` close paren and before
  `prev_review_id = ...`:
```python
            ))
            if persist:
                persist(entries)
            prev_review_id = action.old_entry.review_id
            continue
```

  (b) the post/create/update branch, currently:
```python
        entries.append(review_store.ReviewEntry(
            branch=branch_name,
            position=len(entries) + 1,
            review_id=rid or "",
            subject=review_store.strip_prefix(action.new_commit.subject),
            diff_hash=action.new_commit.diff_hash,
            published=bool(publish),
        ))
        prev_review_id = rid
```
  Insert after that append:
```python
        ))
        if persist:
            persist(entries)
        prev_review_id = rid
```

- [ ] **Step 5: Run the test** —
  `cd /home/kostik/git-helpers && uv run --quiet pytest tests/test_rbt_sync.py::test_execute_persists_after_each_action -q`. Expected: PASS.
  Then the whole sync file: `... pytest tests/test_rbt_sync.py -q`.

- [ ] **Step 6: Commit**

```bash
cd /home/kostik/git-helpers
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(sync): persist callback in _execute, called after each action"
```

---

## Task 2: Wire `run` to persist incrementally (and extract `_preserved_entries`)

**Files:** Modify `src/gg/sync.py`; Test `tests/test_rbt_sync.py`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_rbt_sync.py`:

```python
def test_preserved_entries_returns_skipped_discards():
    from gg import matcher, sync
    from gg.review_store import ReviewEntry

    kept = ReviewEntry("feature", 3, "900", "old kept", "hk", published=True)
    actions = [
        # a normal create (not preserved)
        matcher.SyncAction(kind=matcher.ActionKind.CREATE, old_entry=None,
                           new_commit=matcher.NewCommit("a", "new", "h1"),
                           new_position=1),
        # a skipped-discard: SKIP with an old_entry and no new_commit -> preserved
        matcher.SyncAction(kind=matcher.ActionKind.SKIP, old_entry=kept,
                           new_commit=None, new_position=None),
    ]
    preserved = sync._preserved_entries(actions, "feature")
    assert [e.review_id for e in preserved] == ["900"]
    assert preserved[0].subject == "old kept"
    assert preserved[0].diff_hash == "hk"
    assert preserved[0].published is True
```

- [ ] **Step 2: Run, expect fail** —
  `cd /home/kostik/git-helpers && uv run --quiet pytest tests/test_rbt_sync.py::test_preserved_entries_returns_skipped_discards -q`.
  Expected: `AttributeError: module 'gg.sync' has no attribute '_preserved_entries'`.

- [ ] **Step 3: Add the `_preserved_entries` helper** to `src/gg/sync.py` (near the other
  module-level helpers, e.g. just above `def run`). Also ensure `from dataclasses import
  replace` is imported at the top (add it if missing):

```python
def _preserved_entries(
    actions: list[SyncAction], branch_name: str,
) -> list[review_store.ReviewEntry]:
    """Skipped-discard rows (kept, not discarded) to persist alongside actions.

    Positions are placeholders (0); the persist closure reassigns them after the
    action entries.
    """
    out: list[review_store.ReviewEntry] = []
    for a in actions:
        if a.kind == ActionKind.SKIP and a.old_entry and not a.new_commit:
            out.append(review_store.ReviewEntry(
                branch=branch_name,
                position=0,
                review_id=a.old_entry.review_id,
                subject=a.old_entry.subject,
                diff_hash=a.old_entry.diff_hash,
                published=bool(a.old_entry.published),
            ))
    return out
```

- [ ] **Step 4: Rewire `run`.** Locate, in `run`, the block that calls `_execute(...)`,
  then the "Preserve skipped-discard entries" loop, then "Save state". Currently it is:

```python
    print()
    entries = _execute(
        actions,
        branch_name=branch_name,
        tracking=tracking,
        renumber=args.renumber,
        publish=args.publish,
        verbose=args.verbose,
        progress=args.progress or args.verbose,
        dry_run=False,
        explicit_branch=args.branch,
        initial_depends=args.depends_on,
        reviewers=args.users or None,
        groups=args.groups or None,
        no_numbers=args.no_numbers,
        cwd=cwd,
    )

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

    print(_format_summary(actions), file=sys.stderr)

    # Save state
    if entries:
        review_store.save_reviews(entries, cwd=cwd)
        new_hashes = {e.diff_hash for e in entries}
        diff_cache.save_hashes(new_hashes, cwd=cwd, branch=branch_name)

    return 0
```

  Replace that entire block with:

```python
    print()
    preserved = _preserved_entries(actions, branch_name)

    def _persist(action_entries: list[review_store.ReviewEntry]) -> None:
        merged = list(action_entries)
        for p in preserved:
            merged.append(replace(p, position=len(merged) + 1))
        review_store.save_reviews(merged, cwd=cwd)
        diff_cache.save_hashes(
            {e.diff_hash for e in merged}, cwd=cwd, branch=branch_name,
        )

    entries = _execute(
        actions,
        branch_name=branch_name,
        tracking=tracking,
        renumber=args.renumber,
        publish=args.publish,
        verbose=args.verbose,
        progress=args.progress or args.verbose,
        dry_run=False,
        explicit_branch=args.branch,
        initial_depends=args.depends_on,
        reviewers=args.users or None,
        groups=args.groups or None,
        no_numbers=args.no_numbers,
        persist=_persist,
        cwd=cwd,
    )

    print(_format_summary(actions), file=sys.stderr)

    # Final backstop: the in-loop persist already wrote completed actions; this
    # also covers an all-skipped run where the loop never persisted. save_reviews
    # returns early on an empty list, matching the previous `if entries:` guard.
    _persist(entries)

    return 0
```

- [ ] **Step 5: Run tests** — first the new unit test, then the whole sync suite, then the
  full project suite:

```bash
cd /home/kostik/git-helpers
uv run --quiet pytest tests/test_rbt_sync.py::test_preserved_entries_returns_skipped_discards -q
uv run --quiet pytest tests/test_rbt_sync.py -q
uv run --quiet pytest -q
```
  Expected: all pass (the existing `test_reviews_db_updated_after_sync` and the
  skipped-discard / orphan tests confirm the end-state DB is unchanged).

- [ ] **Step 6: Commit**

```bash
cd /home/kostik/git-helpers
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(sync): persist reviews.db after each action so Ctrl-C keeps progress"
```

---

## Self-Review

- **Spec coverage:** persist callback in `_execute` after each entry (Task 1);
  `run` closure merging preserved entries + `save_reviews`/`save_hashes` (Task 2);
  end-of-run backstop (Task 2 final `_persist`); atomicity is inherent in
  `save_reviews` (unchanged); residual one-action lag is inherent (no code needed);
  scope limited to `_execute`/`run` (no `--close`/`--adopt` change). All covered.
- **Placeholder scan:** none — every step shows the exact code/commands.
- **Type consistency:** `persist: Callable[[list[review_store.ReviewEntry]], None] | None`
  defined in Task 1 and supplied in Task 2; `_preserved_entries(actions, branch_name)
  -> list[ReviewEntry]` defined and called consistently; `replace`/`Callable` imports
  noted. The Task 1 test calls `_execute(..., persist=..., cwd=...)` exactly as the
  signature defines.
- **Note:** Task 1's test avoids network by stubbing `sync.post_one` and
  `sync.rb_api.fetch_reviewers` and passing explicit `reviewers=["rev"]`.
