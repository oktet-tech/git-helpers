# `gg rbt-sync --adopt` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `gg rbt-sync` reconcile against another branch's recorded reviews via `--adopt SRC`, so a refactored branchB can update existing RB reviews originally posted from branchA.

**Architecture:** Single-file change in `src/gg/sync.py`: add two argparse flags (`--adopt SRC`, `--adopt-overwrite`), a validation block in `run()`, and swap the source of the `old` review list. `_execute()` already tags new entries with the current branch, so `save_reviews` naturally writes the result under branchB without touching branchA's rows. No schema changes, no matcher changes.

**Tech Stack:** Python 3.13, existing `gg.sync` / `gg.review_store` / `gg.matcher` modules, pytest.

**Spec:** `docs/superpowers/specs/2026-05-26-rbt-sync-adopt-design.md`.

---

## File Structure

- **Modify**
  - `src/gg/sync.py` — argparse flags, validation block, source-branch swap
  - `tests/test_rbt_sync.py` — new `TestSyncAdopt` test class
  - `README.md` — one row in the `gg rbt-sync` table + a short workflow subsection

`src/gg/rbt.py`, `src/gg/review_store.py`, `src/gg/matcher.py`, and `src/gg/db.py` are intentionally untouched.

---

## Task 1: Argparse flags + happy-path (all-KEEP) test

**Files:**
- Modify: `src/gg/sync.py:22-46` (add_parser), `src/gg/sync.py:244-250` (the old-load block)
- Modify: `tests/test_rbt_sync.py` (new test class at end of file)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rbt_sync.py`:

```python
class TestSyncAdopt:
    def _setup_two_branches(self, git_repo: GitRepo) -> None:
        """branchA tracks master with two commits posted; branchB tracks master with the same commits cherry-picked."""
        git_repo.create_branch("branchA", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        # Capture branchA's HEAD revs to cherry-pick
        full_revs = git_repo.git(
            "log", "--reverse", "--format=%H", "master..HEAD"
        ).stdout.strip().splitlines()
        git_repo.git("checkout", "master")
        git_repo.create_branch("branchB", "master")
        for rev in full_revs:
            git_repo.git("cherry-pick", rev)

    def test_adopt_keep_unchanged(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """branchB has the same diffs as branchA → adopt produces all-keep plan."""
        self._setup_two_branches(git_repo)
        calls_before = rbt_mock.call_count()

        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "branchA")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "keep" in r.stdout
        # Dry-run + identical diffs → no rbt calls at all
        assert rbt_mock.call_count() == calls_before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_keep_unchanged -v`
Expected: FAIL with `gg: error: unrecognized arguments: --adopt branchA`.

- [ ] **Step 3: Add argparse flags**

In `src/gg/sync.py`, find the `--close` argparse block in `add_parser` (~line 42-43):

```python
    p.add_argument("--close", action="store_true",
                   help="close all reviews as submitted and clear DB")
```

Insert immediately after it:

```python
    p.add_argument(
        "--adopt", metavar="SRC", default=None,
        help="reconcile against SRC branch's reviews; save under current branch",
    )
    p.add_argument(
        "--adopt-overwrite", action="store_true",
        help="(with --adopt) overwrite current branch's existing rows",
    )
```

- [ ] **Step 4: Swap the source of the `old` review list**

In `src/gg/sync.py:run()`, find this block (~lines 244-250):

```python
    old = review_store.load_reviews(branch_name, cwd=cwd)
    if not old and not args.new:
        print(
            "[gg] No existing reviews; posting as a fresh series.",
            file=sys.stderr,
        )
        args.new = True
```

Replace it with:

```python
    source_branch = args.adopt or branch_name
    old = review_store.load_reviews(source_branch, cwd=cwd)
    if not args.adopt and not old and not args.new:
        print(
            "[gg] No existing reviews; posting as a fresh series.",
            file=sys.stderr,
        )
        args.new = True
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_keep_unchanged -v`
Expected: PASS.

- [ ] **Step 6: Run the full sync test file to check for regressions**

Run: `uv run pytest tests/test_rbt_sync.py -v`
Expected: all existing tests still pass (the source-swap is a no-op when `--adopt` is absent).

- [ ] **Step 7: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "$(cat <<'EOF'
feat(rbt-sync): --adopt SRC reconciles against another branch's reviews

Lets a refactored branch update existing RB reviews that were
originally posted from a different branch. Saves new entries under
the current branch; SRC's rows are read-only.
EOF
)"
```

---

## Task 2: Source-empty friendly error

**Files:**
- Modify: `src/gg/sync.py:run()` (extend the source-swap block)
- Modify: `tests/test_rbt_sync.py` (add to `TestSyncAdopt`)

- [ ] **Step 1: Write the failing test**

Append to `class TestSyncAdopt`:

```python
    def test_adopt_empty_source_errors(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--adopt against a branch with no DB rows is a friendly error, not a traceback."""
        git_repo.create_branch("branchB", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "nothing-here")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "nothing-here" in r.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_empty_source_errors -v`
Expected: FAIL — currently `--adopt` against an empty source loads `old=[]` and falls into the create-everything path (returncode 0).

- [ ] **Step 3: Add the empty-source error**

In `src/gg/sync.py:run()`, find the block just modified in Task 1:

```python
    source_branch = args.adopt or branch_name
    old = review_store.load_reviews(source_branch, cwd=cwd)
    if not args.adopt and not old and not args.new:
        print(
            "[gg] No existing reviews; posting as a fresh series.",
            file=sys.stderr,
        )
        args.new = True
```

Extend it to:

```python
    source_branch = args.adopt or branch_name
    old = review_store.load_reviews(source_branch, cwd=cwd)
    if args.adopt and not old:
        print(
            f"[gg] no reviews to adopt from branch '{args.adopt}'",
            file=sys.stderr,
        )
        return 1
    if not args.adopt and not old and not args.new:
        print(
            "[gg] No existing reviews; posting as a fresh series.",
            file=sys.stderr,
        )
        args.new = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_empty_source_errors -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(rbt-sync): friendly error when --adopt source has no reviews"
```

---

## Task 3: Conflict refusal when current branch already has rows

**Files:**
- Modify: `src/gg/sync.py:run()` (extend the validation block)
- Modify: `tests/test_rbt_sync.py` (add to `TestSyncAdopt`)

- [ ] **Step 1: Write the failing test**

Append to `class TestSyncAdopt`:

```python
    def test_adopt_conflict_refuses_without_overwrite(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """If branchB already has DB rows, --adopt refuses without --adopt-overwrite."""
        self._setup_two_branches(git_repo)
        # Seed branchB with its own DB rows by posting independently.
        _post_series(git_repo)
        assert git_repo.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "branchB"

        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "branchA")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "adopt-overwrite" in r.stderr
        assert "branchB" in r.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_conflict_refuses_without_overwrite -v`
Expected: FAIL — the run currently proceeds because nothing checks existing rows on the target branch.

- [ ] **Step 3: Add the conflict-refusal logic**

In `src/gg/sync.py:run()`, locate the block just modified in Task 2:

```python
    source_branch = args.adopt or branch_name
    old = review_store.load_reviews(source_branch, cwd=cwd)
    if args.adopt and not old:
        print(
            f"[gg] no reviews to adopt from branch '{args.adopt}'",
            file=sys.stderr,
        )
        return 1
```

Insert an additional check immediately after the empty-source error (before the `if not args.adopt and not old and not args.new:` block):

```python
    if args.adopt:
        existing = review_store.load_reviews(branch_name, cwd=cwd)
        if existing and not args.adopt_overwrite:
            print(
                f"[gg] branch '{branch_name}' already has {len(existing)} reviews; "
                f"pass --adopt-overwrite to replace",
                file=sys.stderr,
            )
            return 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_conflict_refuses_without_overwrite -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(rbt-sync): --adopt refuses to clobber existing rows on target branch"
```

---

## Task 4: `--adopt-overwrite` proceeds

**Files:**
- Modify: `tests/test_rbt_sync.py` (add to `TestSyncAdopt`)

(No `sync.py` change needed — the refusal check from Task 3 is already gated on `not args.adopt_overwrite`. This task verifies the override path.)

- [ ] **Step 1: Write the failing test**

Append to `class TestSyncAdopt`:

```python
    def test_adopt_overwrite_proceeds(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--adopt-overwrite lets us replace existing rows on the target branch."""
        self._setup_two_branches(git_repo)
        _post_series(git_repo)  # branchB gets its own DB rows
        assert git_repo.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "branchB"

        r = git_repo.run_gg(
            "rbt-sync", "-d", "--adopt", "branchA", "--adopt-overwrite",
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        # Dry-run with identical diffs → all keep
        assert "keep" in r.stdout
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_overwrite_proceeds -v`
Expected: PASS — the refusal from Task 3 is bypassed by `--adopt-overwrite`. This is the "verify the override gate works" companion to Task 3.

(Per TDD: this test would have failed if Task 3's gate were absent, so it's a meaningful guard. Treat the passing test as the green half of the cycle that began in Task 3.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_rbt_sync.py
git commit -m "test(rbt-sync): --adopt-overwrite bypasses target-conflict refusal"
```

---

## Task 5: Mutual-exclusion validations

**Files:**
- Modify: `src/gg/sync.py:run()` (insert validation block early)
- Modify: `tests/test_rbt_sync.py` (add to `TestSyncAdopt`)

- [ ] **Step 1: Write the failing tests**

Append to `class TestSyncAdopt`:

```python
    def test_adopt_self_branch_errors(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "feature")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "current branch" in r.stderr

    def test_adopt_incompatible_with_new(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("branchB", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "branchA", "--new")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "incompatible" in r.stderr.lower()

    def test_adopt_overwrite_without_adopt_errors(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("branchB", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d", "--adopt-overwrite")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "--adopt" in r.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt -v -k "self_branch or incompatible or overwrite_without"`
Expected: FAIL — no validation exists yet.

- [ ] **Step 3: Add the validation block**

In `src/gg/sync.py:run()`, find:

```python
def run(args: argparse.Namespace) -> int:
    """Execute the rbt-sync subcommand."""
    cwd = Path.cwd()
    branch_name = git.branchname(cwd=cwd)

    if args.close:
```

Insert the validation between `branch_name = git.branchname(cwd=cwd)` and `if args.close:`:

```python
    if args.adopt_overwrite and not args.adopt:
        print("[gg] --adopt-overwrite requires --adopt", file=sys.stderr)
        return 1
    if args.adopt and (args.new or args.close):
        print("[gg] --adopt is incompatible with --new/--close", file=sys.stderr)
        return 1
    if args.adopt == branch_name:
        print(
            f"[gg] cannot adopt from current branch '{branch_name}'",
            file=sys.stderr,
        )
        return 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt -v -k "self_branch or incompatible or overwrite_without"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gg/sync.py tests/test_rbt_sync.py
git commit -m "feat(rbt-sync): validate --adopt against self/--new/--close and lone --adopt-overwrite"
```

---

## Task 6: End-to-end amended-commits flow

**Files:**
- Modify: `tests/test_rbt_sync.py` (add to `TestSyncAdopt`)

- [ ] **Step 1: Write the failing test**

Append to `class TestSyncAdopt`:

```python
    def test_adopt_update_amended(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """Amend a branchB commit; --adopt branchA updates the same RB review IDs."""
        self._setup_two_branches(git_repo)
        from gg import review_store
        a_entries = review_store.load_reviews("branchA", cwd=git_repo.work_dir)
        assert len(a_entries) == 2
        a_review_ids = [e.review_id for e in a_entries]

        # Amend branchB's last commit so its diff differs from branchA's.
        (git_repo.work_dir / "amended").write_text("amended\n")
        git_repo.git("add", "amended")
        git_repo.git("commit", "--amend", "--no-edit")

        calls_before = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "--adopt", "branchA")
        assert r.returncode == 0, f"stderr: {r.stderr}"

        # Exactly one `rbt post -r <ID>` call against the last review ID.
        new_calls = rbt_mock.calls()[calls_before:]
        update_calls = [c for c in new_calls if "-r" in c]
        assert len(update_calls) == 1
        idx = update_calls[0].index("-r")
        assert update_calls[0][idx + 1] == a_review_ids[1]

        # branchB now has its own DB rows pointing at the same review IDs as branchA.
        b_entries = review_store.load_reviews("branchB", cwd=git_repo.work_dir)
        assert [e.review_id for e in b_entries] == a_review_ids

        # branchA's rows are untouched.
        a_after = review_store.load_reviews("branchA", cwd=git_repo.work_dir)
        assert [e.review_id for e in a_after] == a_review_ids
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_update_amended -v`
Expected: PASS — the end-to-end behavior should already work given the changes from Tasks 1–5.

(Per TDD: this is the integration test that proves the feature actually behaves correctly. If it fails, debug the matcher/save path; do not weaken the assertions.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_rbt_sync.py
git commit -m "test(rbt-sync): end-to-end --adopt across amended commits"
```

---

## Task 7: Dry-run preserves both branches' DB rows

**Files:**
- Modify: `tests/test_rbt_sync.py` (add to `TestSyncAdopt`)

- [ ] **Step 1: Write the test**

Append to `class TestSyncAdopt`:

```python
    def test_adopt_dry_writes_nothing(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--adopt --dry reads SRC but does not mutate either branch's DB rows."""
        self._setup_two_branches(git_repo)
        from gg import review_store
        a_before = review_store.load_reviews("branchA", cwd=git_repo.work_dir)
        b_before = review_store.load_reviews("branchB", cwd=git_repo.work_dir)
        assert b_before == []  # branchB has no rows yet

        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "branchA")
        assert r.returncode == 0, f"stderr: {r.stderr}"

        a_after = review_store.load_reviews("branchA", cwd=git_repo.work_dir)
        b_after = review_store.load_reviews("branchB", cwd=git_repo.work_dir)
        assert a_after == a_before
        assert b_after == []
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run pytest tests/test_rbt_sync.py::TestSyncAdopt::test_adopt_dry_writes_nothing -v`
Expected: PASS — dry-run returns before the save_reviews call (sync.py returns at the `if args.dry: return 0` guard).

- [ ] **Step 3: Run the full test suite to check for regressions**

Run: `uv run pytest tests/ -v 2>&1 | tail -5`
Expected: all tests pass (was 279 before this feature; ~287 after the eight new tests).

- [ ] **Step 4: Commit**

```bash
git add tests/test_rbt_sync.py
git commit -m "test(rbt-sync): --adopt --dry leaves both source and target DB rows untouched"
```

---

## Task 8: README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the flag row to the rbt-sync table**

In `README.md`, find:

```markdown
| `git gg rbt-sync --close` | Close all reviews as submitted and clear the DB |
| `git gg rbt-sync --upstream <ref>` | Override `@{u}` for the diff base and `--tracking-branch` (also on `gg rbt`) |
```

Insert immediately after the `--upstream` line:

```markdown
| `git gg rbt-sync --adopt branchA` | Reconcile against `branchA`'s reviews and save under the current branch |
| `git gg rbt-sync --adopt branchA --adopt-overwrite` | Same, but overwrite the current branch's existing DB rows |
```

- [ ] **Step 2: Add a workflow subsection**

In `README.md`, find the "Typical workflows" section. After the existing workflow subsections (e.g. after the "Multiple patches" or "Syncing a modified series" subsection), add a new subsection:

```markdown
### Refactoring with the original branch as a reference

When you want to keep the original branch around while reworking the
same series on a new branch — for example to compare an original vs
a cleaned-up version — create the new branch tracking the same
upstream and use `--adopt`:

```shell
# branchA: posted to RB, got feedback
git checkout master
git gowork branchB              # new branch tracking origin/main
git cherry-pick branchA~1 branchA
# edit, amend, reorder...

git gg rbt-sync --adopt branchA
```

After adopt, branchB owns the review thread; subsequent
`gg rbt-sync` runs on branchB no longer need `--adopt`. branchA's
DB rows are read-only during adopt and remain in place, but they
still reference the review IDs you just updated — treat branchA as
frozen, and don't run `gg rbt-sync` from it again.
```

(Adjust the placement to wherever it reads best in the file's flow.)

- [ ] **Step 3: Verify markdown renders sensibly**

Run: `head -120 README.md | tail -50` (or open in a markdown viewer)
Expected: the new table row aligns; the new subsection is well-placed.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document --adopt / --adopt-overwrite on rbt-sync"
```

---

## Self-review checklist (already applied)

- [x] **Spec coverage:** all 8 surface/behavior items in the spec map to tasks (flags → Task 1; source-empty → Task 2; conflict → Tasks 3 & 4; validations → Task 5; amended end-to-end → Task 6; dry → Task 7; docs → Task 8).
- [x] **Placeholders:** none; every step has the exact code and command.
- [x] **Type consistency:** flag names (`--adopt`, `--adopt-overwrite`), variable names (`source_branch`, `existing`), and error wording match across tasks.
