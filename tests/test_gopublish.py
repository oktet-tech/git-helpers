"""Tests for git_gopublish -- publish branch to personal namespace."""

import os

from tests.conftest import GitRepo


class TestGopublish:
    def test_dry_run_shows_user_prefix(self, git_repo: GitRepo) -> None:
        git_repo.create_branch("my-feature", "master")
        r = git_repo.run_gitgo("git_gopublish", "-d")
        user = os.environ.get("USER", os.getlogin())
        assert f"user/{user}/my-feature" in r.stdout

    def test_initial_includes_upstream_flag(self, git_repo: GitRepo) -> None:
        git_repo.create_branch("my-feature", "master")
        r = git_repo.run_gitgo("git_gopublish", "-d", "--initial")
        assert "-u" in r.stdout

    def test_already_prefixed_branch_not_doubled(self, git_repo: GitRepo) -> None:
        user = os.environ.get("USER", os.getlogin())
        prefixed = f"user/{user}/feature"
        git_repo.create_branch(prefixed, "master")
        r = git_repo.run_gitgo("git_gopublish", "-d")
        # Destination should be user/X/feature, not user/X/user/X/feature
        assert f"user/{user}/user/{user}" not in r.stdout

    def test_help_flag(self, git_repo: GitRepo) -> None:
        git_repo.create_branch("my-feature", "master")
        r = git_repo.run_gitgo("git_gopublish", "-h")
        assert r.returncode == 0
        assert "gopublish" in r.stdout or "publish" in r.stdout.lower()


class TestPublishMarksState:
    def test_publish_marks_entries_published(
        self, git_repo: GitRepo, rbt_mock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        # Seed drafts via gg rbt (no -p)
        r = git_repo.run_gg("rbt")
        assert r.returncode == 0
        assert all(
            not e.published
            for e in review_store.load_reviews("feature", cwd=git_repo.work_dir)
        )

        r = git_repo.run_gg("publish")
        assert r.returncode == 0
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert entries and all(e.published for e in entries)


class TestPublishAlreadyPublished:
    def test_api_100_is_soft_no_op(self, git_repo, rbt_mock) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt")  # seed one draft
        assert r.returncode == 0

        rbt_mock.queue_failure(
            output=(
                "ERROR: Error publishing review request (it may already be "
                "published): Object does not exist (API Error 100: Does Not "
                "Exist)\n"
            ),
            returncode=1,
            count=1,
        )
        r = git_repo.run_gg("publish")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "already published" in r.stderr
