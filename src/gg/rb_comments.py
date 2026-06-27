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
