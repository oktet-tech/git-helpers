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
