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
