"""Tests for the gg comments subcommand."""

from __future__ import annotations

import subprocess
import sys

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


def test_all_reviews_failed_returns_error(
    git_repo: GitRepo, rbt_mock: RbtMock,
) -> None:
    """Fix 1: when every review fetch fails, return 1 and don't say 'No open issues'."""
    _seed_branch(git_repo, [_entry(1, "1000", "alpha")])
    # No seeded comments; fail the very first api-get (the fetch_review call)
    rbt_mock.queue_failure(output="boom", returncode=1, count=1)
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 1
    assert "could not read" in r.stderr
    assert "No open issues" not in r.stdout


def test_default_output_resolves_to_repo_root(
    git_repo: GitRepo, rbt_mock: RbtMock,
) -> None:
    """Fix 3: relative output path is resolved from repo root, not subprocess cwd."""
    _seed_branch(git_repo, [_entry(1, "1000", "alpha")])
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "x", "issue_opened": True, "issue_status": "open",
             "first_line": 1, "num_lines": 1, "filediff": {"dest_file": "a.py"}}]}])
    subdir = git_repo.work_dir / "src"
    subdir.mkdir()
    r = subprocess.run(
        [sys.executable, "-m", "gg", "comments"],
        cwd=subdir,
        env=git_repo._env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (git_repo.work_dir / ".gg" / "review-comments.md").exists()
    assert not (subdir / ".gg" / "review-comments.md").exists()


def test_empty_text_diff_bullet_no_trailing_space(
    git_repo: GitRepo, rbt_mock: RbtMock,
) -> None:
    """Fix 4: diff comment with empty text produces no trailing space on bullet."""
    _seed_branch(git_repo, [_entry(1, "1000", "alpha")])
    rbt_mock.seed_review_comments("1000", reviews=[
        {"id": 5, "user": "u", "general_comments": [], "diff_comments": [
            {"id": 1, "text": "", "issue_opened": True, "issue_status": "open",
             "first_line": 10, "num_lines": 1, "filediff": {"dest_file": "a.py"}}]}])
    r = git_repo.run_gg("comments", "-o", "-")
    assert r.returncode == 0, r.stderr
    bullet = next(
        line for line in r.stdout.splitlines() if line.startswith("- L10")
    )
    assert not bullet.endswith(" "), repr(bullet)
    assert bullet.endswith(":")


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
