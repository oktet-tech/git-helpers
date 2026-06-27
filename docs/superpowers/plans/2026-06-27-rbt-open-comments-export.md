# `gg comments` — export open ReviewBoard issues — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `gg comments` subcommand that collects every open issue across the current branch's ReviewBoard review series and writes them to one markdown file grouped by source file.

**Architecture:** Two new modules mirror the existing `rb_api.py`/subcommand split: `rb_comments.py` (fetch layer over `rbt api-get`) and `comments.py` (the subcommand + markdown formatting). The `rbt_mock` test fixture is extended to serve the RB comment resources. No production behavior outside these files changes (only `rb_api.fetch_review` gains a backward-compatible `absolute_url` key, and `cli.py`/`gitconfig.go` get one registration line each).

**Tech Stack:** Python 3 (stdlib: argparse, subprocess, json, dataclasses), pytest with the `git_repo`/`rbt_mock` fixtures in `tests/conftest.py`.

## Global Constraints

- ReviewBoard only; review ids come from `review_store.load_reviews(branch)`; comments are read via `rbt api-get`.
- Scope is **open issues only**: a comment with `issue_opened == true` and `issue_status == "open"`. Resolved/dropped issues and non-issue comments are excluded.
- Output is markdown grouped by source file; general (non-diff) comments go in their own `## General` section.
- Command name is `gg comments`; git alias `git rbt-comments`.
- Default output is `.gg/review-comments.md` (the `.gg/` dir is already gitignored); `-o FILE` overrides; `-o -` writes to stdout.
- Read-only — no remote mutations, no issue-status changes.
- A single review's `api-get` failure warns to stderr and continues; it must not abort the whole export.
- Zero open issues → print `No open issues 🎉`, exit 0. No reviews for the branch → friendly stderr error, exit 1.
- Use `from __future__ import annotations` and type hints, matching the existing files.

---

### Task 1: Fetch layer (`rb_comments.py`) + mock support

**Files:**
- Create: `src/gg/rb_comments.py`
- Modify: `src/gg/rb_api.py` (add `absolute_url` to `fetch_review`'s result)
- Modify: `tests/conftest.py` (extend `_MOCK_RBT_SCRIPT` api-get routing; add `state_dir` field + `seed_review_comments` to `RbtMock`)
- Test: `tests/test_rb_comments.py` (new)

**Interfaces:**
- Consumes: `rb_api.fetch_review(review_id, *, cwd)` (returns a dict; this task adds an `"absolute_url"` key). `rbt api-get <path>` is the RB API transport.
- Produces:
  - `Issue` dataclass with fields `review_id: str`, `review_url: str`, `file: str | None`, `first_line: int | None`, `num_lines: int | None`, `text: str`, `author: str`, `kind: str` (`"diff"` | `"general"`).
  - `fetch_open_issues(review_id: str, *, cwd: Path | None = None) -> list[Issue]`.
  - `RbtMock.seed_review_comments(review_id, *, reviews)` test helper, where `reviews` is a list of `{id, user, diff_comments, general_comments}`; each diff comment is `{id, text, issue_opened, issue_status, first_line, num_lines, filediff: {dest_file}}`; each general comment is `{id, text, issue_opened, issue_status}`.

- [ ] **Step 1: Extend the `rbt_mock` fixture to serve comment resources**

In `tests/conftest.py`, replace the entire `if cmd == "api-get":` block inside `_MOCK_RBT_SCRIPT` (currently lines ~176-191, the block that ends just before `elif cmd == "close":`) with:

```python
if cmd == "api-get":
    path = sys.argv[2] if len(sys.argv) > 2 else ""
    path_only = path.split("?")[0].rstrip("/")
    nums = re.findall(r"/(\\d+)", path_only)
    rr_id = nums[0] if nums else "0"
    state_file = os.path.join(STATE_DIR, rr_id + ".json")
    state = {}
    if os.path.exists(state_file):
        with open(state_file) as f:
            state = json.load(f)
    reviews = state.get("reviews", [])

    def _find(oid):
        return next((r for r in reviews if str(r["id"]) == str(oid)), {})

    if path_only.endswith("diff-comments"):
        rv = _find(nums[1]) if len(nums) > 1 else {}
        print(json.dumps({"diff_comments": rv.get("diff_comments", [])}))
    elif path_only.endswith("general-comments"):
        rv = _find(nums[1]) if len(nums) > 1 else {}
        print(json.dumps({"general_comments": rv.get("general_comments", [])}))
    elif path_only.endswith("reviews"):
        out = [
            {"id": r["id"], "links": {"user": {"title": r.get("user", "")}}}
            for r in reviews
        ]
        print(json.dumps({"reviews": out}))
    else:
        rr = {
            "id": int(rr_id),
            "summary": state.get("summary", ""),
            "blocks": [],
            "target_people": [{"title": p} for p in state.get("people", [])],
            "target_groups": [{"title": g} for g in state.get("groups", [])],
            "absolute_url": state.get(
                "absolute_url", f"https://reviews.example.com/r/{rr_id}/"
            ),
        }
        print(json.dumps({"review_request": rr}))
```

(Note `\\d` keeps the backslash inside the Python-string-that-is-a-script. The post path that writes `{people, groups, summary}` state is unchanged, so its `state.get("reviews", [])` is simply `[]`.)

- [ ] **Step 2: Add `state_dir` and `seed_review_comments` to `RbtMock`**

In `tests/conftest.py`, add a `state_dir` field to the `RbtMock` dataclass (after `log_file`):

```python
@dataclass
class RbtMock:
    """Mock rbt executable that logs invocations."""

    script_dir: Path
    log_file: Path
    state_dir: Path
```

Add this method to `RbtMock` (after `queue_failure`):

```python
    def seed_review_comments(self, review_id: str, *, reviews: list[dict]) -> None:
        """Seed comment fixtures for a review request.

        reviews: list of {id, user, diff_comments, general_comments}. Each
        diff_comment: {id, text, issue_opened, issue_status, first_line,
        num_lines, filediff: {dest_file}}. Each general_comment: {id, text,
        issue_opened, issue_status}.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state_file = self.state_dir / f"{review_id}.json"
        state: dict[str, Any] = {}
        if state_file.exists():
            state = json.loads(state_file.read_text())
        state["reviews"] = reviews
        state_file.write_text(json.dumps(state))
```

Then pass `state_dir` when constructing `RbtMock` in the `rbt_mock` fixture:

```python
    return RbtMock(script_dir=mock_dir, log_file=log_file, state_dir=state_dir)
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_rb_comments.py`:

```python
"""Tests for gg.rb_comments -- fetching open RB issue comments."""

from __future__ import annotations

import os

from gg.rb_comments import Issue, fetch_open_issues
from tests.conftest import GitRepo, RbtMock


def _on_path(rbt_mock: RbtMock, monkeypatch) -> None:
    monkeypatch.setenv(
        "PATH", str(rbt_mock.script_dir) + os.pathsep + os.environ["PATH"]
    )


def test_open_diff_issue_collected(
    git_repo: GitRepo, rbt_mock: RbtMock, monkeypatch,
) -> None:
    rbt_mock.seed_review_comments("100", reviews=[
        {"id": 5, "user": "ark-oleg", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "prefer raising", "issue_opened": True,
             "issue_status": "open", "first_line": 142, "num_lines": 1,
             "filediff": {"dest_file": "src/gg/sync.py"}},
        ]},
    ])
    _on_path(rbt_mock, monkeypatch)
    issues = fetch_open_issues("100", cwd=git_repo.work_dir)
    assert len(issues) == 1
    i = issues[0]
    assert isinstance(i, Issue)
    assert i.file == "src/gg/sync.py"
    assert i.first_line == 142 and i.num_lines == 1
    assert i.author == "ark-oleg"
    assert i.kind == "diff"
    assert i.text == "prefer raising"
    assert i.review_id == "100"
    assert i.review_url.endswith("/r/100/")


def test_resolved_dropped_and_non_issue_skipped(
    git_repo: GitRepo, rbt_mock: RbtMock, monkeypatch,
) -> None:
    rbt_mock.seed_review_comments("100", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "resolved", "issue_opened": True,
             "issue_status": "resolved", "first_line": 10, "num_lines": 1,
             "filediff": {"dest_file": "a.py"}},
            {"id": 2, "text": "dropped", "issue_opened": True,
             "issue_status": "dropped", "first_line": 11, "num_lines": 1,
             "filediff": {"dest_file": "a.py"}},
            {"id": 3, "text": "just a note", "issue_opened": False,
             "issue_status": "", "first_line": 12, "num_lines": 1,
             "filediff": {"dest_file": "a.py"}},
            {"id": 4, "text": "open one", "issue_opened": True,
             "issue_status": "open", "first_line": 13, "num_lines": 2,
             "filediff": {"dest_file": "a.py"}},
        ]},
    ])
    _on_path(rbt_mock, monkeypatch)
    issues = fetch_open_issues("100", cwd=git_repo.work_dir)
    assert len(issues) == 1
    assert issues[0].text == "open one"
    assert issues[0].num_lines == 2


def test_general_open_issue_collected(
    git_repo: GitRepo, rbt_mock: RbtMock, monkeypatch,
) -> None:
    rbt_mock.seed_review_comments("100", reviews=[
        {"id": 5, "user": "u", "diff_comments": [], "general_comments": [
            {"id": 9, "text": "add docstring", "issue_opened": True,
             "issue_status": "open"},
            {"id": 10, "text": "resolved general", "issue_opened": True,
             "issue_status": "resolved"},
        ]},
    ])
    _on_path(rbt_mock, monkeypatch)
    issues = fetch_open_issues("100", cwd=git_repo.work_dir)
    assert len(issues) == 1
    assert issues[0].kind == "general"
    assert issues[0].file is None
    assert issues[0].first_line is None
    assert issues[0].text == "add docstring"


def test_multiple_reviews_aggregated(
    git_repo: GitRepo, rbt_mock: RbtMock, monkeypatch,
) -> None:
    rbt_mock.seed_review_comments("100", reviews=[
        {"id": 5, "user": "a", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "x", "issue_opened": True, "issue_status": "open",
             "first_line": 1, "num_lines": 1, "filediff": {"dest_file": "a.py"}},
        ]},
        {"id": 6, "user": "b", "general_comments": [], "diff_comments": [
            {"id": 2, "text": "y", "issue_opened": True, "issue_status": "open",
             "first_line": 2, "num_lines": 1, "filediff": {"dest_file": "b.py"}},
        ]},
    ])
    _on_path(rbt_mock, monkeypatch)
    issues = fetch_open_issues("100", cwd=git_repo.work_dir)
    assert {i.file for i in issues} == {"a.py", "b.py"}
    assert {i.author for i in issues} == {"a", "b"}
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rb_comments.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gg.rb_comments'` (the module does not exist yet).

- [ ] **Step 5: Add `absolute_url` to `rb_api.fetch_review`**

In `src/gg/rb_api.py`, in `fetch_review`, add one key to the returned dict (use `.get` so existing callers/tests with no `absolute_url` don't break):

```python
    return {
        "id": str(rr["id"]),
        "summary": rr["summary"],
        "blocks": [_parse_block_id(b) for b in rr.get("blocks", [])],
        "target_people": [p["title"] for p in rr.get("target_people", [])],
        "target_groups": [g["title"] for g in rr.get("target_groups", [])],
        "absolute_url": rr.get("absolute_url", ""),
    }
```

- [ ] **Step 6: Create `src/gg/rb_comments.py`**

```python
"""Fetch open issue comments from ReviewBoard via `rbt api-get`."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gg import rb_api


@dataclass
class Issue:
    """One open issue comment on a review request."""

    review_id: str
    review_url: str
    file: str | None       # dest_file for diff comments; None for general
    first_line: int | None
    num_lines: int | None
    text: str
    author: str
    kind: str              # "diff" | "general"


def _api_get(path: str, *, cwd: Path | None = None) -> dict:
    """Run `rbt api-get <path>` and return parsed JSON."""
    r = subprocess.run(
        ["rbt", "api-get", path],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise SystemExit(f"rbt api-get failed for {path}: {msg}")
    return json.loads(r.stdout)


def _is_open_issue(comment: dict) -> bool:
    return bool(comment.get("issue_opened")) and comment.get("issue_status") == "open"


def fetch_open_issues(review_id: str, *, cwd: Path | None = None) -> list[Issue]:
    """Return all open-issue comments (diff + general) for one review request."""
    review_url = rb_api.fetch_review(review_id, cwd=cwd).get("absolute_url", "")
    reviews = _api_get(
        f"/review-requests/{review_id}/reviews/", cwd=cwd,
    ).get("reviews", [])

    issues: list[Issue] = []
    for review in reviews:
        oid = review["id"]
        author = review.get("links", {}).get("user", {}).get("title", "")

        diff = _api_get(
            f"/review-requests/{review_id}/reviews/{oid}/diff-comments/"
            f"?expand=filediff",
            cwd=cwd,
        ).get("diff_comments", [])
        for c in diff:
            if not _is_open_issue(c):
                continue
            filediff = c.get("filediff") or {}
            issues.append(Issue(
                review_id=str(review_id),
                review_url=review_url,
                file=filediff.get("dest_file"),
                first_line=c.get("first_line"),
                num_lines=c.get("num_lines"),
                text=c.get("text", ""),
                author=author,
                kind="diff",
            ))

        general = _api_get(
            f"/review-requests/{review_id}/reviews/{oid}/general-comments/",
            cwd=cwd,
        ).get("general_comments", [])
        for c in general:
            if not _is_open_issue(c):
                continue
            issues.append(Issue(
                review_id=str(review_id),
                review_url=review_url,
                file=None,
                first_line=None,
                num_lines=None,
                text=c.get("text", ""),
                author=author,
                kind="general",
            ))
    return issues
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rb_comments.py -v`
Expected: PASS (4 tests).

- [ ] **Step 8: Run the full suite to confirm no regressions**

Run: `uv run pytest tests/ -q`
Expected: all pass (the mock change is additive; the post path's state has no `reviews` key, so existing rbt/sync/import tests are unaffected).

- [ ] **Step 9: Commit**

```bash
git add src/gg/rb_comments.py src/gg/rb_api.py tests/conftest.py tests/test_rb_comments.py
git commit -m "feat(rb-comments): fetch open RB issue comments for a review request"
```

---

### Task 2: `gg comments` subcommand + formatting + wiring

**Files:**
- Create: `src/gg/comments.py`
- Modify: `src/gg/cli.py` (import + register)
- Modify: `gitconfig.go` (add `rbt-comments` alias)
- Test: `tests/test_comments.py` (new)

**Interfaces:**
- Consumes: `gg.rb_comments.fetch_open_issues`, `gg.rb_comments.Issue`; `gg.review_store.load_reviews(branch, *, cwd)`, `gg.review_store.ReviewEntry`; `gg.git.branchname(cwd=...)`.
- Produces: `comments.add_parser(subparsers)`, `comments.run(args)`, `comments.format_markdown(issues, *, branch, review_count)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_comments.py`:

```python
"""Tests for the gg comments subcommand."""

from __future__ import annotations

from gg import review_store
from tests.conftest import GitRepo, RbtMock


def _entry(pos: int, rid: str, subject: str) -> review_store.ReviewEntry:
    return review_store.ReviewEntry("feature", pos, rid, subject, f"h{pos}")


def _seed_branch(git_repo: GitRepo, entries) -> None:
    git_repo.create_branch("feature", "master")
    review_store.save_reviews(entries, cwd=git_repo.work_dir)


def test_groups_by_file_and_general_to_stdout(
    git_repo: GitRepo, rbt_mock: RbtMock,
) -> None:
    _seed_branch(git_repo, [_entry(1, "1000", "alpha"), _entry(2, "1001", "beta")])
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "ark-oleg", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "prefer raising here", "issue_opened": True,
             "issue_status": "open", "first_line": 142, "num_lines": 1,
             "filediff": {"dest_file": "src/gg/sync.py"}},
        ]},
    ])
    rbt_mock.seed_review_comments("1001", reviews=[
        {"id": 6, "user": "ark-alxk", "diff_comments": [], "general_comments": [
            {"id": 9, "text": "add a module docstring", "issue_opened": True,
             "issue_status": "open"},
        ]},
    ])
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "## src/gg/sync.py" in out
    assert "L142 (r/1000, by ark-oleg): prefer raising here" in out
    assert "## General" in out
    assert "(r/1001, by ark-alxk): add a module docstring" in out
    assert "/r/1000/" in out
    assert "2 open across 2 reviews" in out


def test_line_span_rendered(git_repo: GitRepo, rbt_mock: RbtMock) -> None:
    _seed_branch(git_repo, [_entry(1, "1000", "alpha")])
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "dedupe", "issue_opened": True, "issue_status": "open",
             "first_line": 160, "num_lines": 4, "filediff": {"dest_file": "a.py"}},
        ]},
    ])
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 0, r.stderr
    assert "L160-163 (r/1000, by u): dedupe" in r.stdout


def test_default_output_file_written(git_repo: GitRepo, rbt_mock: RbtMock) -> None:
    _seed_branch(git_repo, [_entry(1, "1000", "alpha")])
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "x", "issue_opened": True, "issue_status": "open",
             "first_line": 1, "num_lines": 1, "filediff": {"dest_file": "a.py"}},
        ]},
    ])
    r = git_repo.run_gg("comments")
    assert r.returncode == 0, r.stderr
    out_file = git_repo.work_dir / ".gg" / "review-comments.md"
    assert out_file.exists()
    assert "## a.py" in out_file.read_text()
    assert str(out_file) in r.stdout  # path is printed


def test_no_reviews_errors(git_repo: GitRepo, rbt_mock: RbtMock) -> None:
    git_repo.create_branch("feature", "master")
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 1
    assert "No reviews" in r.stderr


def test_zero_open_issues(git_repo: GitRepo, rbt_mock: RbtMock) -> None:
    _seed_branch(git_repo, [_entry(1, "1000", "alpha")])
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "done", "issue_opened": True, "issue_status": "resolved",
             "first_line": 1, "num_lines": 1, "filediff": {"dest_file": "a.py"}},
        ]},
    ])
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 0
    assert "No open issues" in r.stdout


def test_fetch_failure_warns_and_continues(
    git_repo: GitRepo, rbt_mock: RbtMock,
) -> None:
    _seed_branch(git_repo, [_entry(1, "1000", "alpha"), _entry(2, "1001", "beta")])
    rbt_mock.seed_review_comments("1001", reviews=[
        {"id": 6, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "still here", "issue_opened": True,
             "issue_status": "open", "first_line": 5, "num_lines": 1,
             "filediff": {"dest_file": "b.py"}},
        ]},
    ])
    # Fail r/1000's first api-get (its fetch_review), so it is skipped.
    rbt_mock.queue_failure(output="boom", returncode=1, count=1)
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 0
    assert "still here" in r.stdout
    assert "1000" in r.stderr  # warned about the skipped review


def test_empty_review_id_skipped(git_repo: GitRepo, rbt_mock: RbtMock) -> None:
    _seed_branch(git_repo, [_entry(1, "", "orphan"), _entry(2, "1001", "beta")])
    rbt_mock.seed_review_comments("1001", reviews=[
        {"id": 6, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "ok", "issue_opened": True, "issue_status": "open",
             "first_line": 5, "num_lines": 1, "filediff": {"dest_file": "b.py"}},
        ]},
    ])
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 0
    assert "ok" in r.stdout
    assert "no review id" in r.stderr


def test_branch_flag(git_repo: GitRepo, rbt_mock: RbtMock) -> None:
    git_repo.create_branch("feature", "master")
    review_store.save_reviews(
        [review_store.ReviewEntry("other", 1, "1000", "alpha", "h1")],
        cwd=git_repo.work_dir,
    )
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "z", "issue_opened": True, "issue_status": "open",
             "first_line": 1, "num_lines": 1, "filediff": {"dest_file": "a.py"}},
        ]},
    ])
    r = git_repo.run_gg("comments", "-b", "other", "-o", "-")
    assert r.returncode == 0, r.stderr
    assert "z" in r.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_comments.py -v`
Expected: FAIL — `gg comments` is an unknown subcommand (argparse error, non-zero exit), so the assertions on stdout/return codes fail.

- [ ] **Step 3: Create `src/gg/comments.py`**

```python
"""The `gg comments` subcommand -- export open RB issues to a file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gg import git, review_store
from gg.rb_comments import Issue, fetch_open_issues


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the comments subcommand."""
    p = subparsers.add_parser(
        "comments", help="export open ReviewBoard issues to a file",
    )
    p.add_argument(
        "-o", "--output", default=".gg/review-comments.md",
        help="output file ('-' for stdout); default .gg/review-comments.md",
    )
    p.add_argument(
        "-b", "--branch", default=None, help="branch (default: current)",
    )
    p.set_defaults(func=run)


def _line_label(issue: Issue) -> str:
    """L<first_line> or L<first>-<last> for a diff comment; '' for general."""
    if issue.first_line is None:
        return ""
    if issue.num_lines and issue.num_lines > 1:
        return f"L{issue.first_line}-{issue.first_line + issue.num_lines - 1}"
    return f"L{issue.first_line}"


def _emit_body(out: list[str], issue: Issue) -> None:
    """Append continuation text lines and the review URL for one issue."""
    cont = issue.text.splitlines()[1:]
    for line in cont:
        out.append(f"  {line}")
    if issue.review_url:
        out.append(f"  {issue.review_url}")


def format_markdown(
    issues: list[Issue], *, branch: str, review_count: int,
) -> str:
    """Render open issues as markdown grouped by source file."""
    diff_issues = [i for i in issues if i.kind == "diff"]
    general_issues = [i for i in issues if i.kind == "general"]

    out: list[str] = [
        f"# Open review issues — branch {branch} "
        f"({len(issues)} open across {review_count} reviews)",
        "",
    ]

    by_file: dict[str, list[Issue]] = {}
    for i in diff_issues:
        by_file.setdefault(i.file or "(unknown file)", []).append(i)
    for fname in sorted(by_file):
        out.append(f"## {fname}")
        for i in sorted(by_file[fname], key=lambda i: (i.first_line or 0)):
            first = i.text.splitlines()[0] if i.text else ""
            out.append(f"- {_line_label(i)} (r/{i.review_id}, by {i.author}): {first}")
            _emit_body(out, i)
        out.append("")

    if general_issues:
        out.append("## General")
        for i in general_issues:
            first = i.text.splitlines()[0] if i.text else ""
            out.append(f"- (r/{i.review_id}, by {i.author}): {first}")
            _emit_body(out, i)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def run(args: argparse.Namespace) -> int:
    """Execute the comments subcommand."""
    cwd = Path.cwd()
    branch = args.branch or git.branchname(cwd=cwd)
    entries = review_store.load_reviews(branch, cwd=cwd)
    if not entries:
        print(f"[gg] No reviews for branch '{branch}'.", file=sys.stderr)
        return 1

    all_issues: list[Issue] = []
    read = 0
    skipped = 0
    for e in entries:
        if not e.review_id:
            print(
                f"[gg] skipping entry with no review id: {e.subject}",
                file=sys.stderr,
            )
            continue
        try:
            all_issues.extend(fetch_open_issues(e.review_id, cwd=cwd))
            read += 1
        except SystemExit as exc:
            print(f"[gg] skipping r/{e.review_id}: {exc}", file=sys.stderr)
            skipped += 1

    if not all_issues:
        print("No open issues 🎉")
        return 0

    text = format_markdown(all_issues, branch=branch, review_count=read)

    if args.output == "-":
        sys.stdout.write(text)
    else:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = cwd / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text)
        print(f"Wrote {len(all_issues)} open issue(s) to {out_path}")

    if skipped:
        print(f"[gg] {skipped} review(s) could not be read.", file=sys.stderr)
    return 0
```

(The zero-open message prints to stdout so it is visible whether or not `-o -` was passed; the test only checks `"No open issues" in r.stdout`.)

- [ ] **Step 4: Register the subcommand in `cli.py`**

In `src/gg/cli.py`, add `comments` to the import and register it. Change the import line:

```python
from gg import comments, db, publish, rbt, rbt_import, sync
```

and add the registration alongside the others (before `db.add_parser(sub)`):

```python
    comments.add_parser(sub)
    db.add_parser(sub)
```

- [ ] **Step 5: Add the `git rbt-comments` alias**

In `gitconfig.go`, add this line in the `[alias]` block next to the existing `rbt` alias (after line 23):

```
	rbt-comments = "!PATH=$HOME/.local/bin:$PATH; gg comments"
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_comments.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/gg/comments.py src/gg/cli.py gitconfig.go tests/test_comments.py
git commit -m "feat(comments): add gg comments to export open RB issues"
```

---

### Task 3: Document `gg comments` in the README

**Files:**
- Modify: `README.md` (command table near the `rbt-sync` rows; a short prose section)

**Interfaces:** none.

- [ ] **Step 1: Add a command-table row**

In `README.md`, immediately after the `| `git gg rbt-sync --close` | ...` row (the line containing "Close all reviews as submitted and clear the DB"), add:

```markdown
| `git rbt-comments` | Export the branch's open ReviewBoard issues to `.gg/review-comments.md` (`-o -` for stdout) |
```

- [ ] **Step 2: Add a prose section**

In `README.md`, immediately before the `### Importing an existing ReviewBoard chain` heading, add:

```markdown
### Collecting open review comments

After reviewers file issues across the series, gather every open issue into a
single file to hand to an LLM (or to read through):

```shell
# Write .gg/review-comments.md (grouped by source file) and print its path
git rbt-comments

# Or stream to stdout / a pipe
git rbt-comments -o -
```

Only open issues are included — resolved/dropped issues and non-issue
comments are skipped. The file lists each issue with its `file:line`, the
reviewer, the review id, and a link, so an agent with the repo checked out
can open each location and address it.
```

- [ ] **Step 3: Verify the additions reference the real command**

Run: `grep -n "rbt-comments" README.md`
Expected: at least two matches (the table row and the prose example).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document git rbt-comments"
```

---

## Self-Review

**Spec coverage:**
- RB backend, ids from `reviews.db` → Task 2 `run` uses `review_store.load_reviews`; comments via `rbt api-get` in Task 1. ✓
- Open-issues-only scope → `_is_open_issue` (Task 1); tested in `test_resolved_dropped_and_non_issue_skipped`, `test_zero_open_issues`. ✓
- Markdown grouped by file + General section → `format_markdown` (Task 2); tested. ✓
- Command name `gg comments` / `git rbt-comments` → Task 2 Steps 4–5. ✓
- Default `.gg/review-comments.md`, `-o` override, `-o -` stdout → Task 2 parser + `run`; tested in `test_default_output_file_written` and the `-o -` tests. ✓
- `-b/--branch` → Task 2; tested `test_branch_flag`. ✓
- Two-module split + `absolute_url` on `fetch_review` → Tasks 1. ✓
- Per-review failure warns + continues → `run` try/except; tested `test_fetch_failure_warns_and_continues`. ✓
- No reviews → exit 1; zero open → exit 0 message → tested. ✓
- Empty review_id skipped → `run` guard; tested `test_empty_review_id_skipped`. ✓
- Line single vs span rendering → `_line_label`; tested `test_line_span_rendered`. ✓
- Read-only / no mutation → only `api-get` (GET) calls and local file write. ✓
- Testing via extended `rbt_mock` → Task 1 Steps 1–2. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code; every run step states expected output. ✓

**Type consistency:** `Issue` fields and `fetch_open_issues` signature defined in Task 1 are consumed unchanged in Task 2. `seed_review_comments(review_id, *, reviews)` defined in Task 1 Step 2 and used in Tasks 1 & 2 tests. `format_markdown(issues, *, branch, review_count)` defined and used within Task 2. `review_store.ReviewEntry(branch, position, review_id, subject, diff_hash)` positional order matches the dataclass. RB JSON shapes produced by the mock (`reviews`/`diff_comments`/`general_comments`/`filediff.dest_file`/`links.user.title`) match what `fetch_open_issues` reads. ✓
