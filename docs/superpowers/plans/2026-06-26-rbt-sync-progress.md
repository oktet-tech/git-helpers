# `gg rbt-sync --progress` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--progress` flag to `gg rbt-sync` that prints one prose line per entry as a real run executes, mirroring the existing `gg rbt --progress` style.

**Architecture:** Add a `--progress` argparse flag in `src/gg/sync.py`. Compute `show_progress = args.progress or args.verbose` in `run()` and pass it as a new `progress: bool` parameter into `_execute()`. Inside `_execute()`, emit a bold one-liner for every entry at the point each action is processed — discards in phase 1, everything else in phase 2 — using the existing `_BOLD`/`_RESET` constants and the `[i/N]` strings already produced by `assign_numbers`.

**Tech Stack:** Python 3 (argparse, stdlib), pytest with the `git_repo` + `rbt_mock` fixtures in `tests/conftest.py`.

## Global Constraints

- Use `from __future__ import annotations` and type hints throughout (already present in `sync.py`).
- Progress lines go to **stdout** via `print(...)`; the `Synced: …` summary stays on stderr (unchanged).
- No new short flag — `--progress` is long-only (`-p` is already `--publish`).
- Do not change error handling, the plan table, the summary line, dry-run output, or `rbt.py`.
- The `posting …` line prints **before** the network call; the `  -> created/updated/published r/<id>` line prints **after**, using the resolved review id.
- Subjects shown come from `action.new_commit.subject` (non-discard) or `action.old_entry.subject` (discard), matching the plan table.

---

### Task 1: Core progress output (flag + post/create/update/discard/keep/skip)

Adds the `--progress` flag, threads `progress` into `_execute()`, and prints the non-publish progress lines. The `publish (unchanged)` line is added in Task 2.

**Files:**
- Modify: `src/gg/sync.py` (parser in `add_parser`, `_execute` signature + body, `run()` call site)
- Test: `tests/test_rbt_sync.py` (new `TestProgress` class)

**Interfaces:**
- Consumes: `ActionKind` (`KEEP`, `UPDATE`, `KEEP_DEP`, `CREATE`, `DISCARD`, `SKIP`) from `gg.matcher`; `assign_numbers(actions, renumber=...)` returning a list of `(SyncAction, num_str)` tuples where `num_str` is like `"[1/3]"` or `"--"`; `_BOLD`, `_RESET` module constants.
- Produces: `_execute(..., progress: bool, ...)` — new keyword parameter inserted immediately after `verbose`. `run()` computes `show_progress = args.progress or args.verbose` and passes `progress=show_progress`.

- [ ] **Step 1: Write the failing tests**

Add this class to the end of `tests/test_rbt_sync.py`:

```python
class TestProgress:
    def test_progress_prints_per_action_lines(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--progress emits one prose line per entry: keep/update/discard/create."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")        # will stay KEEP
        git_repo.commit("beta")         # will be amended -> UPDATE
        git_repo.commit("gamma")        # will be dropped -> DISCARD
        _post_series(git_repo)

        # Drop gamma, amend beta, add delta -> series is alpha, beta', delta
        git_repo.git("reset", "--hard", "HEAD~1")           # drop gamma
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")      # amend beta
        git_repo.commit("delta")                            # new -> create

        r = git_repo.run_gg("rbt-sync", "--progress")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        out = _plain(r.stdout)
        assert "keep (unchanged): alpha" in out, out
        assert re.search(r"posting.*beta", out), out
        assert re.search(r"-> updated r/\d+", out), out
        assert re.search(r"discard r/\d+: gamma", out), out
        assert re.search(r"posting.*delta", out), out
        assert re.search(r"-> created r/\d+", out), out

    def test_no_progress_lines_without_flag(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """A plain real run stays quiet between the plan table and the summary."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")
        _post_series(git_repo)

        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")      # amend beta -> update

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0
        out = _plain(r.stdout)
        # The plan table uses the words keep/update, but never these phrases:
        assert "(unchanged)" not in out
        assert "posting" not in out
        assert "->" not in out

    def test_verbose_implies_progress(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--verbose alone produces the progress one-liners (verbose => progress)."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")
        _post_series(git_repo)

        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")      # amend beta -> update

        r = git_repo.run_gg("rbt-sync", "-v")
        assert r.returncode == 0
        out = _plain(r.stdout)
        assert re.search(r"posting.*beta", out), out
        assert re.search(r"-> updated r/\d+", out), out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rbt_sync.py::TestProgress -v`
Expected: FAIL. `test_progress_prints_per_action_lines` and `test_verbose_implies_progress` fail (no progress output); `--progress` is also an unknown argparse option so that test's `run_gg` returns non-zero.

- [ ] **Step 3: Add the `--progress` flag to the parser**

In `src/gg/sync.py`, in `add_parser`, add the flag immediately after the `-v/--verbose` line:

```python
    p.add_argument("-v", "--verbose", action="store_true", help="show rbt output")
    p.add_argument("--progress", action="store_true", help="print one line per action")
```

- [ ] **Step 4: Add the `progress` parameter to `_execute`**

In `src/gg/sync.py`, change the `_execute` signature to add `progress` right after `verbose`:

```python
def _execute(
    actions: list[SyncAction],
    *,
    branch_name: str,
    tracking: str,
    renumber: bool,
    publish: bool,
    verbose: bool,
    progress: bool,
    dry_run: bool,
    explicit_branch: str | None,
    initial_depends: str | None,
    reviewers: list[str] | None = None,
    groups: list[str] | None = None,
    no_numbers: bool = False,
    cwd: Path,
) -> list[review_store.ReviewEntry]:
```

- [ ] **Step 5: Print discard lines in phase 1**

In `_execute`, replace the phase-1 discard loop:

```python
    # Phase 1: discard removed reviews
    for action, _ in numbered:
        if action.kind == ActionKind.DISCARD and action.old_entry:
            close_discarded(
                action.old_entry.review_id,
                dry_run=dry_run, verbose=verbose, cwd=cwd,
            )
```

with:

```python
    # Phase 1: discard removed reviews
    for action, _ in numbered:
        if action.kind == ActionKind.DISCARD and action.old_entry:
            if progress:
                print(
                    f"{_BOLD}discard r/{action.old_entry.review_id}: "
                    f"{action.old_entry.subject}{_RESET}",
                    flush=True,
                )
            close_discarded(
                action.old_entry.review_id,
                dry_run=dry_run, verbose=verbose, cwd=cwd,
            )
```

- [ ] **Step 6: Print a skip line in phase 2**

In `_execute`, replace the top-of-loop skip guard:

```python
    for action, num_str in numbered:
        if action.kind in (ActionKind.DISCARD, ActionKind.SKIP):
            continue
```

with:

```python
    for action, num_str in numbered:
        if action.kind == ActionKind.DISCARD:
            continue
        if action.kind == ActionKind.SKIP:
            if progress:
                subj = (
                    action.new_commit.subject if action.new_commit
                    else action.old_entry.subject if action.old_entry
                    else ""
                )
                print(f"{_BOLD}skip: {subj}{_RESET}")
            continue
```

- [ ] **Step 7: Print the KEEP no-op line**

In `_execute`, inside the KEEP no-op block, replace:

```python
            assert action.old_entry is not None
            entry_published = bool(action.old_entry.published)
            if publish and not entry_published:
                rc = publish_one(
                    action.old_entry.review_id,
                    dry_run=dry_run, verbose=verbose, cwd=cwd,
                )
                if rc == 0:
                    entry_published = True
```

with (Task 2 fills in the `publish (unchanged)` prints; for now only the plain keep line):

```python
            assert action.old_entry is not None
            entry_published = bool(action.old_entry.published)
            if publish and not entry_published:
                rc = publish_one(
                    action.old_entry.review_id,
                    dry_run=dry_run, verbose=verbose, cwd=cwd,
                )
                if rc == 0:
                    entry_published = True
            elif progress:
                print(f"{_BOLD}keep (unchanged): {action.new_commit.subject}{_RESET}")
```

- [ ] **Step 8: Print the posting / created / updated lines**

In `_execute`, find the `needs_fresh_post` block. Immediately before `if needs_fresh_post:` add the "posting" line, and after the `if/else` that resolves `rid` add the result line. The region becomes:

```python
        if progress:
            pos = f" {num_str}" if num_str != "--" else ""
            print(
                f"{_BOLD}posting{pos}: {action.new_commit.subject} ...{_RESET}",
                flush=True,
            )

        if needs_fresh_post:
            if reviewers is not None or groups is not None:
                create_reviewers = reviewers or []
                create_groups = groups or []
            elif prev_review_id:
                create_reviewers, create_groups = rb_api.fetch_reviewers(
                    prev_review_id, cwd=cwd,
                )
            else:
                create_reviewers, create_groups = [], []
            result = post_one(
                action.new_commit.rev, tracking,
                first_post=True,
                publish=publish,
                dry_run=dry_run,
                verbose=verbose,
                reviewers=create_reviewers,
                groups=create_groups,
                explicit_branch=explicit_branch,
                num_string=num_prefix,
                depends_on=prev_review_id,
                cwd=cwd,
            )
            rid = result.review_id
        else:
            # UPDATE or KEEP_DEP with a real review_id: re-post with -r ID
            assert action.old_entry is not None
            result = post_one(
                action.new_commit.rev, tracking,
                review_id=action.old_entry.review_id,
                publish=publish,
                dry_run=dry_run,
                verbose=verbose,
                explicit_branch=explicit_branch,
                num_string=num_prefix,
                depends_on=prev_review_id,
                cwd=cwd,
            )
            rid = result.review_id or action.old_entry.review_id

        if progress and rid:
            verb = "created" if needs_fresh_post else "updated"
            print(f"{_BOLD}  -> {verb} r/{rid}{_RESET}")
```

(Only two things are new versus the existing code: the leading `if progress: … posting …` block, and the trailing `if progress and rid: … -> created/updated …` block. The `post_one` calls and `rid` assignments are unchanged.)

- [ ] **Step 9: Wire `show_progress` in `run()`**

In `src/gg/sync.py`, in `run()`, update the `_execute(...)` call to pass `progress`. Change:

```python
    print()
    entries = _execute(
        actions,
        branch_name=branch_name,
        tracking=tracking,
        renumber=args.renumber,
        publish=args.publish,
        verbose=args.verbose,
        dry_run=False,
        explicit_branch=args.branch,
        initial_depends=args.depends_on,
        reviewers=args.users or None,
        groups=args.groups or None,
        no_numbers=args.no_numbers,
        cwd=cwd,
    )
```

to:

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
```

- [ ] **Step 10: Run the Task 1 tests to verify they pass**

Run: `uv run pytest tests/test_rbt_sync.py::TestProgress::test_progress_prints_per_action_lines tests/test_rbt_sync.py::TestProgress::test_no_progress_lines_without_flag tests/test_rbt_sync.py::TestProgress::test_verbose_implies_progress -v`
Expected: PASS (3 passed).

- [ ] **Step 11: Run the full suite to confirm no regressions**

Run: `uv run pytest tests/ -q`
Expected: all pass (the new `TestProgress::test_progress_publish_unchanged_line` does not exist yet, so it is not collected).

- [ ] **Step 12: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(rbt-sync): add --progress flag for per-action logging"
```

---

### Task 2: `publish (unchanged)` progress line

Adds progress output for the `gg rbt-sync -p` path that publishes an unchanged KEEP review that is still an unpublished draft.

**Files:**
- Modify: `src/gg/sync.py` (`_execute` KEEP no-op `publish` branch)
- Test: `tests/test_rbt_sync.py` (`TestProgress`)

**Interfaces:**
- Consumes: the `progress` parameter added to `_execute` in Task 1; `publish_one(review_id, dry_run=..., verbose=..., cwd=...)` returning an int rc (0 == success); `action.old_entry.review_id`, `action.new_commit.subject`.
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

Add to `TestProgress` in `tests/test_rbt_sync.py`:

```python
    def test_progress_publish_unchanged_line(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--progress -p on an unpublished KEEP draft logs publish + result."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        _post_series(git_repo)  # gg rbt without -p -> draft, review id r/1000

        r = git_repo.run_gg("rbt-sync", "-p", "--progress")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        out = _plain(r.stdout)
        assert "publish (unchanged): alpha" in out, out
        assert "-> published r/1000" in out, out
        # It is a publish, not a keep-noop or a re-post
        assert "keep (unchanged): alpha" not in out
        assert "posting" not in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_rbt_sync.py::TestProgress::test_progress_publish_unchanged_line -v`
Expected: FAIL — `publish (unchanged): alpha` and `-> published r/1000` are not printed (Task 1 only prints in the `elif progress` keep branch).

- [ ] **Step 3: Add the publish-unchanged prints**

In `src/gg/sync.py`, in the KEEP no-op `publish` branch, change:

```python
            if publish and not entry_published:
                rc = publish_one(
                    action.old_entry.review_id,
                    dry_run=dry_run, verbose=verbose, cwd=cwd,
                )
                if rc == 0:
                    entry_published = True
            elif progress:
                print(f"{_BOLD}keep (unchanged): {action.new_commit.subject}{_RESET}")
```

to:

```python
            if publish and not entry_published:
                if progress:
                    print(
                        f"{_BOLD}publish (unchanged): "
                        f"{action.new_commit.subject}{_RESET}",
                        flush=True,
                    )
                rc = publish_one(
                    action.old_entry.review_id,
                    dry_run=dry_run, verbose=verbose, cwd=cwd,
                )
                if rc == 0:
                    entry_published = True
                    if progress:
                        print(
                            f"{_BOLD}  -> published "
                            f"r/{action.old_entry.review_id}{_RESET}"
                        )
            elif progress:
                print(f"{_BOLD}keep (unchanged): {action.new_commit.subject}{_RESET}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestProgress::test_progress_publish_unchanged_line -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(rbt-sync): log publish (unchanged) under --progress"
```

---

### Task 3: Document `--progress` in the README

**Files:**
- Modify: `README.md` (command table around line 62; prose example block around line 182)

**Interfaces:** none.

- [ ] **Step 1: Add a command-table row**

In `README.md`, immediately after the `git gg rbt-sync -f` row (the "Re-post every matched commit, ignoring the diff-hash cache" line), add:

```markdown
| `git gg rbt-sync --progress` | Print one line per action during a real run (implied by `-v`) |
```

- [ ] **Step 2: Add a prose example**

In `README.md`, in the `gg rbt-sync` example block, after the `git gg rbt-sync -f` example and its comment, add:

```shell

# Watch a real run tick by -- one prose line per action
# (posting/created/updated/keep/discard/publish). Implied by -v.
git gg rbt-sync --progress
```

- [ ] **Step 3: Verify the docs render and reference the real flag**

Run: `grep -n "rbt-sync --progress" README.md`
Expected: two matches (the table row and the example).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document gg rbt-sync --progress"
```

---

## Self-Review

**Spec coverage:**
- `--progress` flag, long-only, help text → Task 1 Step 3. ✓
- `show_progress = args.progress or args.verbose` (verbose implies progress) → Task 1 Step 9; tested in `test_verbose_implies_progress`. ✓
- Only affects real runs (dry returns before `_execute`) → unchanged; `test_no_progress_lines_without_flag` guards the no-flag real run. ✓
- stdout for progress, stderr for summary → all prints use `print(...)` (stdout); summary untouched. ✓
- Vocabulary table (discard / skip / keep / publish / create / update) → Task 1 Steps 5–8, Task 2 Step 3. Every row covered. ✓
- `posting` before call, `-> …` after → Task 1 Step 8, Task 2 Step 3. ✓
- Two-phase ordering caveat (discards grouped first) → discards print in phase 1 (Step 5), rest in phase 2. ✓
- Non-goals (no change to error handling, table, summary, rbt.py) → respected; edits are additive prints + one parameter. ✓
- Testing: mixed plan, quiet-without-flag, verbose-implies, publish-unchanged → Task 1 Step 1, Task 2 Step 1. ✓
- README documentation → Task 3. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every command has expected output. ✓

**Type consistency:** `progress: bool` defined in Task 1 Step 4 and passed in Step 9; used identically in Tasks 1–2. `num_str` is the `assign_numbers` tuple element (`"[i/N]"` or `"--"`), matched by the `!= "--"` guard. `rid` is the existing local already set in both branches. ✓
