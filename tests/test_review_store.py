"""Tests for gg.review_store -- sqlite review metadata storage."""

import sqlite3

from gg import review_store
from gg.review_store import ReviewEntry, strip_prefix
from tests.conftest import GitRepo, RbtMock


class TestStripPrefix:
    def test_strips_integer_prefix(self) -> None:
        assert strip_prefix("[1/3]: fix crash") == "fix crash"

    def test_strips_fractional_prefix(self) -> None:
        assert strip_prefix("[2.1/5]: new helper") == "new helper"

    def test_no_prefix(self) -> None:
        assert strip_prefix("fix crash") == "fix crash"

    def test_empty(self) -> None:
        assert strip_prefix("") == ""

    def test_bracket_in_middle(self) -> None:
        assert strip_prefix("fix [1/3] thing") == "fix [1/3] thing"


class TestReviewCRUD:
    def test_load_empty(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        result = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert result == []

    def test_save_and_load(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        entries = [
            ReviewEntry("feature", 1, "1000", "fix crash", "aaa"),
            ReviewEntry("feature", 2, "1001", "add tests", "bbb"),
        ]
        review_store.save_reviews(entries, cwd=git_repo.work_dir)
        loaded = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(loaded) == 2
        assert loaded[0].review_id == "1000"
        assert loaded[1].subject == "add tests"

    def test_save_replaces(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        entries1 = [ReviewEntry("feature", 1, "1000", "old", "aaa")]
        review_store.save_reviews(entries1, cwd=git_repo.work_dir)

        entries2 = [ReviewEntry("feature", 1, "2000", "new", "bbb")]
        review_store.save_reviews(entries2, cwd=git_repo.work_dir)

        loaded = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(loaded) == 1
        assert loaded[0].review_id == "2000"

    def test_branches_isolated(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        review_store.save_reviews(
            [ReviewEntry("feat-a", 1, "1000", "a stuff", "aaa")],
            cwd=git_repo.work_dir,
        )
        review_store.save_reviews(
            [ReviewEntry("feat-b", 1, "2000", "b stuff", "bbb")],
            cwd=git_repo.work_dir,
        )
        a = review_store.load_reviews("feat-a", cwd=git_repo.work_dir)
        b = review_store.load_reviews("feat-b", cwd=git_repo.work_dir)
        assert len(a) == 1
        assert a[0].review_id == "1000"
        assert len(b) == 1
        assert b[0].review_id == "2000"


class TestDiffHashCRUD:
    def test_load_empty(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        result = review_store.load_diff_hashes("feature", cwd=git_repo.work_dir)
        assert result == set()

    def test_round_trip(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        hashes = {"aaa", "bbb", "ccc"}
        review_store.save_diff_hashes("feature", hashes, cwd=git_repo.work_dir)
        loaded = review_store.load_diff_hashes("feature", cwd=git_repo.work_dir)
        assert loaded == hashes

    def test_branches_isolated(self, git_repo: GitRepo, rbt_mock: RbtMock) -> None:
        review_store.save_diff_hashes("feat-a", {"aaa"}, cwd=git_repo.work_dir)
        review_store.save_diff_hashes("feat-b", {"bbb"}, cwd=git_repo.work_dir)
        assert review_store.load_diff_hashes("feat-a", cwd=git_repo.work_dir) == {"aaa"}
        assert review_store.load_diff_hashes("feat-b", cwd=git_repo.work_dir) == {"bbb"}


class TestPublishedFlag:
    def test_save_load_round_trips_published(self, git_repo) -> None:
        from gg import review_store
        review_store.save_reviews(
            [
                review_store.ReviewEntry(
                    branch="feature", position=1, review_id="1000",
                    subject="first", diff_hash="a" * 40, published=True,
                ),
                review_store.ReviewEntry(
                    branch="feature", position=2, review_id="1001",
                    subject="second", diff_hash="b" * 40, published=False,
                ),
            ],
            cwd=git_repo.work_dir,
        )
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert [e.published for e in entries] == [True, False]

    def test_default_published_is_false(self, git_repo) -> None:
        from gg import review_store
        e = review_store.ReviewEntry(
            branch="feature", position=1, review_id="1000",
            subject="x", diff_hash="a" * 40,
        )
        assert e.published is False

    def test_migration_backfills_existing_rows_as_published(self, git_repo) -> None:
        import sqlite3
        from gg import review_store
        db = git_repo.work_dir / ".gg" / "reviews.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        # Pre-feature schema: no `published` column.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE reviews ("
            "branch TEXT NOT NULL, position INTEGER NOT NULL, "
            "review_id TEXT NOT NULL, subject TEXT NOT NULL, "
            "diff_hash TEXT NOT NULL, PRIMARY KEY (branch, position))"
        )
        conn.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?)",
            ("feature", 1, "1000", "legacy", "a" * 40),
        )
        conn.commit()
        conn.close()
        # load_reviews → _connect → migration backfills published=1
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(entries) == 1
        assert entries[0].published is True
