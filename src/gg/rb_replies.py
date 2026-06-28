"""ReviewBoard write operations for `gg reply` (the only networked write layer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gg import rb_session

_STATUS = {"resolve": "resolved", "drop": "dropped"}


def _get_review(review_request_id: str, review_oid: int, cwd: Path | None):
    client = rb_session.get_client(cwd)
    return client.get_path(
        f"/review-requests/{review_request_id}/reviews/{review_oid}/"
    )


def post_replies_for_review(
    review_request_id: str, review_oid: int, targets: list[dict[str, Any]],
    *, cwd: Path | None = None,
) -> None:
    """Create one reply on the review, add each target's comment-reply, publish.

    targets: list of {"kind": "diff"|"general", "comment_id": int, "text": str}.
    """
    review = _get_review(review_request_id, review_oid, cwd)
    reply = review.get_replies().create()
    for t in targets:
        sink = reply.get_diff_comments() if t["kind"] == "diff" \
            else reply.get_general_comments()
        sink.create(reply_to_id=t["comment_id"], text=t["text"])
    reply.update(public=True)


def set_issue_status(
    review_request_id: str, review_oid: int, kind: str, comment_id: int,
    action: str, *, cwd: Path | None = None,
) -> None:
    """Set a comment's issue_status to resolved/dropped per `action`."""
    client = rb_session.get_client(cwd)
    sub = "diff-comments" if kind == "diff" else "general-comments"
    comment = client.get_path(
        f"/review-requests/{review_request_id}/reviews/{review_oid}/{sub}/{comment_id}/"
    )
    comment.update(issue_status=_STATUS[action])
