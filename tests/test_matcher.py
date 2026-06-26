"""Tests for gg.matcher -- reconciliation algorithm."""

from gg.matcher import ActionKind, NewCommit, SyncAction, reconcile
from gg.review_store import ReviewEntry


def _entry(pos: int, rid: str, subject: str, diff: str = "") -> ReviewEntry:
    return ReviewEntry("feature", pos, rid, subject, diff or f"hash_{pos}")


def _commit(subject: str, diff: str = "", rev: str = "") -> NewCommit:
    return NewCommit(
        rev=rev or subject[:8],
        subject=subject,
        diff_hash=diff or f"new_hash_{subject[:8]}",
    )


class TestExactMatch:
    def test_unchanged_series(self) -> None:
        old = [_entry(0, "100", "fix crash", "h1"), _entry(1, "101", "add tests", "h2")]
        new = [_commit("fix crash", "h1"), _commit("add tests", "h2")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert len(non_discard) == 2
        assert non_discard[0].kind == ActionKind.KEEP
        assert non_discard[1].kind == ActionKind.KEEP

    def test_diff_changed_is_update(self) -> None:
        old = [_entry(0, "100", "fix crash", "h1")]
        new = [_commit("fix crash", "h2")]
        actions = reconcile(old, new)
        assert actions[0].kind == ActionKind.UPDATE
        assert actions[0].old_entry.review_id == "100"

    def test_subject_match_ignores_prefix(self) -> None:
        old = [_entry(0, "100", "[1/2]: fix crash", "h1")]
        new = [_commit("[1/2]: fix crash", "h1")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert non_discard[0].kind == ActionKind.KEEP

    def test_subject_changed_same_diff_is_update(self) -> None:
        """Editing the commit subject without touching the diff triggers UPDATE.

        Fuzzy-matched against the old review (high subject similarity), same
        diff_hash, but the user has reworded -- the new summary must reach RB.
        """
        old = [_entry(0, "100", "fix the crash in parser", "h1")]
        new = [_commit("fix the crash in parser module", "h1")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert len(non_discard) == 1
        assert non_discard[0].kind == ActionKind.UPDATE
        assert non_discard[0].old_entry.review_id == "100"

    def test_subject_change_ignores_prefix_diff(self) -> None:
        """A change only in the [i/N]: prefix is not a real subject edit."""
        old = [_entry(0, "100", "fix crash", "h1")]
        new = [_commit("[1/1]: fix crash", "h1")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert non_discard[0].kind == ActionKind.KEEP


class TestCreateAndDiscard:
    def test_new_commit_creates(self) -> None:
        old = [_entry(0, "100", "fix crash", "h1")]
        new = [_commit("fix crash", "h1"), _commit("new feature")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert len(non_discard) == 2
        assert non_discard[1].kind == ActionKind.CREATE

    def test_dropped_commit_discards(self) -> None:
        old = [_entry(0, "100", "fix crash", "h1"), _entry(1, "101", "dropped", "h2")]
        new = [_commit("fix crash", "h1")]
        actions = reconcile(old, new)
        discards = [a for a in actions if a.kind == ActionKind.DISCARD]
        assert len(discards) == 1
        assert discards[0].old_entry.review_id == "101"

    def test_empty_old_all_create(self) -> None:
        old: list[ReviewEntry] = []
        new = [_commit("a"), _commit("b")]
        actions = reconcile(old, new)
        assert all(a.kind == ActionKind.CREATE for a in actions)
        assert len(actions) == 2

    def test_empty_new_all_discard(self) -> None:
        old = [_entry(0, "100", "a"), _entry(1, "101", "b")]
        new: list[NewCommit] = []
        actions = reconcile(old, new)
        assert all(a.kind == ActionKind.DISCARD for a in actions)
        assert len(actions) == 2


class TestFuzzyMatch:
    def test_similar_subject_matches(self) -> None:
        old = [_entry(0, "100", "fix the crash in parser", "h1")]
        new = [_commit("fix the crash in parser module", "h2")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert len(non_discard) == 1
        assert non_discard[0].kind == ActionKind.UPDATE
        assert non_discard[0].old_entry.review_id == "100"

    def test_very_different_no_match(self) -> None:
        old = [_entry(0, "100", "fix crash", "h1")]
        new = [_commit("completely unrelated feature", "h2")]
        actions = reconcile(old, new)
        discards = [a for a in actions if a.kind == ActionKind.DISCARD]
        creates = [a for a in actions if a.kind == ActionKind.CREATE]
        assert len(discards) == 1
        assert len(creates) == 1


class TestDependencyChain:
    def test_dep_update_on_insert(self) -> None:
        """Inserting before an existing review should mark it needs_dep_update."""
        old = [_entry(0, "100", "first", "h1"), _entry(1, "101", "second", "h2")]
        new = [_commit("first", "h1"), _commit("inserted"), _commit("second", "h2")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert len(non_discard) == 3
        # "second" should need dep update since its predecessor changed
        second_action = non_discard[2]
        assert second_action.old_entry.review_id == "101"
        assert second_action.kind == ActionKind.KEEP_DEP

    def test_no_dep_update_when_unchanged(self) -> None:
        old = [_entry(0, "100", "first", "h1"), _entry(1, "101", "second", "h2")]
        new = [_commit("first", "h1"), _commit("second", "h2")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert all(not a.needs_dep_update for a in non_discard)


class TestPositionTiebreak:
    def test_same_subject_uses_position(self) -> None:
        """Two old entries with same subject -- match by position proximity."""
        old = [_entry(0, "100", "fix bug", "h1"), _entry(1, "101", "fix bug", "h2")]
        new = [_commit("fix bug", "h1"), _commit("fix bug", "h2")]
        actions = reconcile(old, new)
        non_discard = [a for a in actions if a.kind != ActionKind.DISCARD]
        assert non_discard[0].old_entry.review_id == "100"
        assert non_discard[1].old_entry.review_id == "101"


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
