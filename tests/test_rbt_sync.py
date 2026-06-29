"""Tests for gg rbt-sync -- series reconciliation with ReviewBoard."""

import os
import re
import subprocess
import sys
import textwrap

from tests.conftest import GitRepo, RbtMock

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _post_series(git_repo: GitRepo) -> None:
    """Post the current series with gg rbt to seed reviews.db."""
    r = git_repo.run_gg("rbt")
    assert r.returncode == 0, f"gg rbt failed: {r.stderr}"


class TestSyncDryRun:
    def test_unchanged_series_all_keep(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "keep" in out
        # No new rbt calls in dry mode
        assert rbt_mock.call_count() == initial_calls

    def test_amended_commit_shows_update(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        # Amend last commit to change its diff
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "keep" in out
        assert "update" in out

    def test_dropped_commit_shows_discard(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        git_repo.commit("temporary hack")
        _post_series(git_repo)

        # Drop last commit
        git_repo.git("reset", "--hard", "HEAD~1")

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "discard" in out

    def test_inserted_commit_shows_create(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        rev1 = git_repo.commit("fix crash")
        rev2 = git_repo.commit("add tests")
        _post_series(git_repo)

        # Insert a commit between the two via rebase
        full_revs = git_repo.git(
            "log", "--reverse", "--format=%H", "master..HEAD"
        ).stdout.strip().splitlines()

        git_repo.git("checkout", full_revs[0])
        git_repo.commit("inserted helper")
        new_insert = git_repo.git("rev-parse", "HEAD").stdout.strip()
        # Cherry-pick the second commit on top
        git_repo.git("cherry-pick", full_revs[1])
        new_head = git_repo.git("rev-parse", "HEAD").stdout.strip()

        # Point the branch to the new series
        git_repo.git("checkout", "feature")
        git_repo.git("reset", "--hard", new_head)

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "create" in out

    def test_renumber_flag(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        r = git_repo.run_gg("rbt-sync", "-d", "--renumber")
        assert r.returncode == 0
        out = r.stdout
        assert "[1/" in out
        assert "[2/" in out

    def test_upstream_override_no_upstream(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """rbt-sync --upstream works on a branch without @{u}."""
        git_repo.git("checkout", "-b", "no-upstream")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d", "--upstream", "origin/master")
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_missing_upstream_friendly_error(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.git("checkout", "-b", "no-upstream")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "upstream" in r.stderr.lower()

    def test_plan_pub_column_tracks_published_state(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        import re as _re
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)  # draft, published=0

        # Unpublished draft under -p → plan shows "keep ... yes"
        r = git_repo.run_gg("rbt-sync", "-d", "-p")
        assert r.returncode == 0
        assert _re.search(r"keep\s+yes", _plain(r.stdout)), _plain(r.stdout)

        # Publish it, then the plan shows "keep ... --"
        git_repo.run_gg("rbt-sync", "-p")
        r = git_repo.run_gg("rbt-sync", "-d", "-p")
        assert r.returncode == 0
        assert _re.search(r"keep\s+--", _plain(r.stdout)), _plain(r.stdout)


class TestSyncExecution:
    def test_update_posts_changed_only(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Amend last commit
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0
        # Should have posted only the changed review (update) + close none
        new_calls = rbt_mock.call_count() - initial_calls
        assert new_calls == 1  # one rbt post -r call

    def test_discard_calls_rbt_close(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("to be dropped")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Drop last commit
        git_repo.git("reset", "--hard", "HEAD~1")

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0

        # Check that rbt close was called
        all_calls = rbt_mock.calls()
        close_calls = [c for c in all_calls[initial_calls:] if c[0:2] == ["post", "close"] or (len(c) > 1 and c[1] == "close")]
        # The mock logs all rbt calls; close would be ["close", "--close-type=discarded", "ID"]
        new_calls = all_calls[initial_calls:]
        has_close = any("close" in c for c in new_calls)
        assert has_close

    def test_no_existing_reviews_auto_new(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """Empty reviews.db: `gg rbt-sync -d` auto-falls into --new."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        # Plan shows the commit as a CREATE
        assert "create" in r.stdout
        # Stderr notice on the auto path
        assert "No existing reviews; posting as a fresh series" in r.stderr

    def test_auto_new_executes(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """Empty reviews.db: non-dry `gg rbt-sync` posts every commit."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0

        post_calls = [c for c in rbt_mock.calls() if c and c[0] == "post"]
        assert len(post_calls) == 2

        # reviews.db now has two entries
        from gg import review_store
        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(entries) == 2

    def test_explicit_new_no_auto_notice(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """`--new` with populated DB does not emit the auto-new notice."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)

        # Add one more commit so the explicit --new has something to replace
        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync", "--new", "-d")
        assert r.returncode == 0
        assert "No existing reviews; posting as a fresh series" not in r.stderr

    def test_create_posts_new_review(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Add a new commit
        git_repo.commit("new feature")

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0
        new_calls = rbt_mock.call_count() - initial_calls
        # One new post for the created review
        assert new_calls >= 1


    def test_renumber_reposts_stale_prefix(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--renumber re-posts kept reviews whose [i/N] prefix changed."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Add a third commit — old series was [1/2],[2/2], new is [1/3]..[3/3]
        git_repo.commit("new feature")

        r = git_repo.run_gg("rbt-sync", "--renumber")
        assert r.returncode == 0
        all_calls = rbt_mock.calls()[initial_calls:]
        post_calls = [c for c in all_calls if c and c[0] == "post"]
        # 2 re-posts (stale prefix) + 1 create = 3 rbt post calls
        assert len(post_calls) == 3

    def test_renumber_skips_matching_prefix(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--renumber does not re-post when [i/N] already matches."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # No changes — positions [1/2],[2/2] still correct
        r = git_repo.run_gg("rbt-sync", "--renumber")
        assert r.returncode == 0
        assert rbt_mock.call_count() == initial_calls


class TestPlanPublishColumn:
    def test_publish_flag_shows_yes(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """With -p -d, update/create rows and unpublished-draft KEEP rows show 'yes'."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        # Amend last commit to trigger update
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        r = git_repo.run_gg("rbt-sync", "-p", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "Pub" in out
        # Both keep (unpublished draft) and update rows show 'yes' under -p
        for line in out.splitlines():
            if "keep" in line and "keep+dep" not in line:
                assert "yes" in line
            if "update" in line:
                assert "yes" in line

    def test_no_publish_flag_shows_draft(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """Without -p, update/create rows show 'draft'."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "Pub" in out
        for line in out.splitlines():
            if "update" in line:
                assert "draft" in line

    def test_all_keep_no_pub_column(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """When all actions are keep, Pub column is omitted."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        assert "Pub" not in r.stdout


class TestExecutionSummary:
    def test_summary_after_sync(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """After sync execution, stderr has correct counts."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        # Amend last commit (1 keep + 1 update)
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0
        assert "Synced:" in r.stderr
        assert "1 kept" in r.stderr
        assert "1 updated" in r.stderr

    def test_summary_with_create_and_discard(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """Summary includes created and discarded counts."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("to be dropped")
        _post_series(git_repo)

        # Drop last commit and add a new one
        git_repo.git("reset", "--hard", "HEAD~1")
        git_repo.commit("new feature")

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0
        assert "Synced:" in r.stderr
        assert "1 created" in r.stderr
        assert "1 discarded" in r.stderr


class TestReviewerInheritance:
    def test_create_inherits_reviewers(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """New reviews inherit target-people and target-groups from depends-on."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt", "-U", "alice", "-G", "devteam")
        assert r.returncode == 0

        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0

        # Find the post call for the new review (the last post call)
        calls = rbt_mock.calls()
        post_calls = [c for c in calls if c and c[0] == "post"]
        last_post = post_calls[-1]
        assert "--target-people" in last_post
        assert "alice" in last_post
        assert "--target-groups" in last_post
        assert "devteam" in last_post


class TestSyncState:
    def test_reviews_db_updated_after_sync(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        # Add a new commit and sync
        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0

        # Re-running sync should show the updated state
        r2 = git_repo.run_gg("rbt-sync", "-d")
        assert r2.returncode == 0
        out = r2.stdout
        # All three should now be keep (no changes since last sync)
        lines = [l for l in out.splitlines() if "keep" in l]
        assert len(lines) == 3


def _make_editor_script(tmp_path, sed_expr: str) -> str:
    """Create a script that applies a sed expression to the file argument."""
    script = tmp_path / "fake_editor.sh"
    script.write_text(f"#!/bin/sh\nsed -i '{sed_expr}' \"$1\"\n")
    script.chmod(0o755)
    return str(script)


def _make_clear_editor(tmp_path) -> str:
    """Create a script that empties the file (abort)."""
    script = tmp_path / "clear_editor.sh"
    script.write_text("#!/bin/sh\n: > \"$1\"\n")
    script.chmod(0o755)
    return str(script)


class TestInteractiveMode:
    def test_interactive_skip_discard(
        self, git_repo: GitRepo, rbt_mock: RbtMock, tmp_path,
    ) -> None:
        """Editor changes 'discard' to 'skip', review is not closed."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("to be dropped")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Drop last commit so it shows as discard
        git_repo.git("reset", "--hard", "HEAD~1")

        editor = _make_editor_script(tmp_path, "s/^discard/skip   /")
        git_repo._env["EDITOR"] = editor
        # Unset VISUAL so EDITOR is used
        git_repo._env.pop("VISUAL", None)

        r = git_repo.run_gg("rbt-sync", "-i")
        assert r.returncode == 0

        # No close calls should have happened
        all_calls = rbt_mock.calls()
        new_calls = all_calls[initial_calls:]
        has_close = any("close" in c for c in new_calls)
        assert not has_close

        # The skipped entry should be preserved -- next dry-run should
        # still show it as discard
        r2 = git_repo.run_gg("rbt-sync", "-d")
        assert r2.returncode == 0
        assert "discard" in r2.stdout

    def test_interactive_abort(
        self, git_repo: GitRepo, rbt_mock: RbtMock, tmp_path,
    ) -> None:
        """Editor empties file, sync aborts with no execution."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Amend so there's something to sync
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        editor = _make_clear_editor(tmp_path)
        git_repo._env["EDITOR"] = editor
        git_repo._env.pop("VISUAL", None)

        r = git_repo.run_gg("rbt-sync", "-i")
        assert r.returncode == 0
        assert "Aborted" in r.stdout

        # No new rbt calls
        assert rbt_mock.call_count() == initial_calls


class TestExplicitReviewers:
    def test_users_override_inheritance(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """-U overrides reviewer inheritance from depends-on."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.run_gg("rbt", "-U", "alice")

        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync", "-U", "bob")
        assert r.returncode == 0

        calls = rbt_mock.calls()
        post_calls = [c for c in calls if c and c[0] == "post"]
        last_post = post_calls[-1]
        assert "--target-people" in last_post
        assert "bob" in last_post
        # Should NOT have alice (inherited) — bob overrides
        assert "alice" not in last_post

    def test_groups_override(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """-G overrides group inheritance."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.run_gg("rbt", "-G", "team-a")

        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync", "-G", "team-b")
        assert r.returncode == 0

        calls = rbt_mock.calls()
        post_calls = [c for c in calls if c and c[0] == "post"]
        last_post = post_calls[-1]
        assert "--target-groups" in last_post
        assert "team-b" in last_post
        assert "team-a" not in last_post

    def test_reviewers_shown_in_plan(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """Plan output includes reviewer/group header."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        r = git_repo.run_gg("rbt-sync", "-d", "-U", "alice", "-G", "devteam")
        assert r.returncode == 0
        assert "Reviewers: alice" in r.stdout
        assert "Groups: devteam" in r.stdout


class TestNoNumbers:
    def test_no_numbers_suppresses_prefix(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--no-numbers prevents [i/N] prefix on posted reviews."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        # Amend to trigger update
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")

        r = git_repo.run_gg("rbt-sync", "--no-numbers")
        assert r.returncode == 0

        calls = rbt_mock.calls()
        post_calls = [c for c in calls if c and c[0] == "post"]
        last_post = post_calls[-1]
        # The summary should not start with [N/M]:
        summary_args = [a for a in last_post if a.startswith("--summary=")]
        assert summary_args
        summary = summary_args[0].split("=", 1)[1]
        assert not summary.startswith("[")

    def test_no_numbers_on_create(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--no-numbers also works for newly created reviews."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)

        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync", "--no-numbers")
        assert r.returncode == 0

        calls = rbt_mock.calls()
        post_calls = [c for c in calls if c and c[0] == "post"]
        last_post = post_calls[-1]
        summary_args = [a for a in last_post if a.startswith("--summary=")]
        assert summary_args
        summary = summary_args[0].split("=", 1)[1]
        assert not summary.startswith("[")


class TestCloseFlag:
    def test_close_no_reviews_errors(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--close with no DB entries returns 1."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")

        r = git_repo.run_gg("rbt-sync", "--close")
        assert r.returncode == 1
        assert "No reviews to close" in r.stdout

    def test_close_dry_run_shows_plan(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """-d --close prints reviews but makes no rbt calls."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Simulate pushed branch: reset to master so tracking..HEAD is empty
        git_repo.git("reset", "--hard", "master")

        r = git_repo.run_gg("rbt-sync", "-d", "--close")
        assert r.returncode == 0
        assert "close r/" in r.stdout
        # No new rbt calls in dry mode
        assert rbt_mock.call_count() == initial_calls

    def test_close_calls_rbt_close_submitted(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--close calls rbt close --close-type=submitted for each review."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        git_repo.git("reset", "--hard", "master")

        r = git_repo.run_gg("rbt-sync", "--close")
        assert r.returncode == 0

        all_calls = rbt_mock.calls()
        new_calls = all_calls[initial_calls:]
        close_calls = [c for c in new_calls if c and c[0] == "close"]
        assert len(close_calls) == 2
        for c in close_calls:
            assert "--close-type=submitted" in c

    def test_close_clears_db(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """After --close, load_reviews() returns empty."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)

        git_repo.git("reset", "--hard", "master")

        r = git_repo.run_gg("rbt-sync", "--close")
        assert r.returncode == 0

        # Try --close again: should error with "no reviews"
        r2 = git_repo.run_gg("rbt-sync", "--close")
        assert r2.returncode == 1
        assert "No reviews to close" in r2.stdout

    def test_close_empty_range_ok(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--close works even when tracking..HEAD is empty (main use case)."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)

        # Push to origin so tracking..HEAD becomes empty
        git_repo.git("reset", "--hard", "master")

        r = git_repo.run_gg("rbt-sync", "--close")
        assert r.returncode == 0
        assert "Closed 1 review(s) as submitted" in r.stderr


class TestNewFlag:
    def test_new_bypasses_no_reviews_guard(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--new allows sync even when no existing reviews are stored."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")

        r = git_repo.run_gg("rbt-sync", "--new")
        assert r.returncode == 0
        assert "2 created" in r.stderr

    def test_new_creates_all_from_scratch(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--new with existing reviews produces only CREATE actions (no discard)."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        git_repo.commit("temporary hack")
        _post_series(git_repo)

        # Drop last commit — normally would show discard
        git_repo.git("reset", "--hard", "HEAD~1")

        r = git_repo.run_gg("rbt-sync", "--new", "-d")
        assert r.returncode == 0
        out = r.stdout
        assert "create" in out
        assert "discard" not in out

    def test_new_no_close_calls(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--new does not close any old reviews on ReviewBoard."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        git_repo.commit("temporary hack")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        # Drop last commit and sync with --new
        git_repo.git("reset", "--hard", "HEAD~1")

        r = git_repo.run_gg("rbt-sync", "--new")
        assert r.returncode == 0

        all_calls = rbt_mock.calls()
        new_calls = all_calls[initial_calls:]
        has_close = any("close" in c for c in new_calls)
        assert not has_close

    def test_new_replaces_db(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """After --new, re-running sync sees the fresh series (all keep)."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)

        # Add a third commit, sync with --new
        git_repo.commit("new feature")
        r = git_repo.run_gg("rbt-sync", "--new")
        assert r.returncode == 0

        # Re-running plain sync should show all keep
        r2 = git_repo.run_gg("rbt-sync", "-d")
        assert r2.returncode == 0
        lines = [l for l in r2.stdout.splitlines() if "keep" in l]
        assert len(lines) == 3


class TestPublishUnchangedOnSync:
    """rbt-sync -p must publish KEEP drafts, not just no-op them."""

    def test_publish_publishes_kept_reviews(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()
        # Initial _post_series uses `gg rbt` without -p; reviews are drafts.

        r = git_repo.run_gg("rbt-sync", "-p")
        assert r.returncode == 0

        new_calls = rbt_mock.calls()[initial_calls:]
        publish_calls = [c for c in new_calls if c and c[0] == "publish"]
        # Both KEEP entries get an explicit rbt publish call
        assert len(publish_calls) == 2
        published_ids = sorted(c[1] for c in publish_calls)
        assert published_ids == ["1000", "1001"]

    def test_no_publish_when_flag_absent(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)
        initial_calls = rbt_mock.call_count()

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0

        new_calls = rbt_mock.calls()[initial_calls:]
        assert [c for c in new_calls if c and c[0] == "publish"] == []

    def test_keep_publish_skips_already_published(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """First -p publishes the drafts; a second -p makes no publish call."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)  # gg rbt without -p → drafts (published=0)

        n0 = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "-p")
        assert r.returncode == 0
        pub1 = [c for c in rbt_mock.calls()[n0:] if c and c[0] == "publish"]
        assert len(pub1) == 2  # both drafts published

        n1 = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "-p")
        assert r.returncode == 0
        pub2 = [c for c in rbt_mock.calls()[n1:] if c and c[0] == "publish"]
        assert pub2 == []  # already published → no re-publish


class TestEmptyReviewIdRecovery:
    """When reviews.db has entries with empty review_id (a previous post
    failed mid-flight), --force re-posts them as fresh posts. The reviewer
    args must flow through, since the resulting rbt call has no `-r ID` and
    is therefore a first-post that requires/accepts reviewers."""

    def test_force_repost_passes_reviewers_when_review_id_empty(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")

        # Seed reviews.db with a row whose review_id is empty (recovery state)
        review_store.save_reviews(
            [
                review_store.ReviewEntry(
                    branch="feature", position=1, review_id="",
                    subject="fix crash",
                    diff_hash="0" * 40,
                ),
            ],
            cwd=git_repo.work_dir,
        )

        r = git_repo.run_gg("rbt-sync", "-U", "alice", "-p", "--force")
        assert r.returncode == 0, f"stderr: {r.stderr}"

        post_calls = [c for c in rbt_mock.calls() if c and c[0] == "post"]
        assert len(post_calls) == 1
        # Must be a fresh post (no -r) and must include --target-people alice
        assert "-r" not in post_calls[0]
        assert "--target-people" in post_calls[0]
        idx = post_calls[0].index("--target-people")
        assert post_calls[0][idx + 1] == "alice"


class TestSyncRetry:
    def test_keep_publish_retries_after_207(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo._env["GG_RBT_MISSING_BASE_DELAY"] = "0"
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)

        rbt_mock.queue_failure(
            output="Error Message: The file was not found in the repository.\n"
                   "API Code: code: 207\n",
            returncode=1,
            count=1,
        )
        r = git_repo.run_gg("rbt-sync", "-p")
        assert r.returncode == 0
        publish_calls = [c for c in rbt_mock.calls() if c and c[0] == "publish"]
        assert len(publish_calls) == 2

    def test_keep_publish_gives_up_after_4_attempts(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo._env["GG_RBT_MISSING_BASE_DELAY"] = "0"
        git_repo._env["GG_RBT_MISSING_BASE_RETRIES"] = "3"

        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)

        rbt_mock.queue_failure(
            output="Error Message: The file was not found in the repository.\n"
                   "API Code: code: 207\n",
            returncode=1,
            count=10,
        )
        r = git_repo.run_gg("rbt-sync", "-p")
        # rbt-sync's KEEP branch calls publish_one without propagating its
        # exit code (current behavior), so the overall command may still
        # succeed even when publish fails. But the 4 attempts must have
        # been made.
        publish_calls = [c for c in rbt_mock.calls() if c and c[0] == "publish"]
        # 4 attempts: 1 initial + 3 retries
        assert len(publish_calls) == 4


class TestForceFlag:
    def test_force_converts_keep_to_update(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial = rbt_mock.call_count()

        r = git_repo.run_gg("rbt-sync", "-f")
        assert r.returncode == 0

        new_calls = rbt_mock.calls()[initial:]
        post_calls = [c for c in new_calls if c and c[0] == "post"]
        # Both kept commits get re-posted with -r <id>
        assert len(post_calls) == 2
        for c in post_calls:
            assert any(arg == "-r" for arg in c), c

    def test_force_keeps_create_and_discard(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("to drop")
        _post_series(git_repo)
        initial = rbt_mock.call_count()

        # Drop the second commit; add a new one
        git_repo.git("reset", "--hard", "HEAD~1")
        git_repo.commit("new feature")

        r = git_repo.run_gg("rbt-sync", "-f")
        assert r.returncode == 0

        new_calls = rbt_mock.calls()[initial:]
        post_calls = [c for c in new_calls if c and c[0] == "post"]
        close_calls = [c for c in new_calls if c and c[0] == "close"]
        # 1 forced update (the kept "fix crash") + 1 create (the new commit)
        assert len(post_calls) == 2
        # 1 discard of the dropped review
        assert len(close_calls) == 1

    def test_force_with_publish_publishes_each(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        git_repo.commit("add tests")
        _post_series(git_repo)
        initial = rbt_mock.call_count()

        r = git_repo.run_gg("rbt-sync", "-f", "-p")
        assert r.returncode == 0

        new_calls = rbt_mock.calls()[initial:]
        post_calls = [c for c in new_calls if c and c[0] == "post"]
        publish_calls = [c for c in new_calls if c and c[0] == "publish"]
        # 2 forced re-posts, each with -p
        assert len(post_calls) == 2
        for c in post_calls:
            assert "-p" in c, c
        # No separate rbt publish calls (UPDATE+-p covers it)
        assert publish_calls == []

    def test_force_header_appears_in_plan(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        _post_series(git_repo)
        r = git_repo.run_gg("rbt-sync", "-f", "-d")
        assert r.returncode == 0
        assert "Force: yes" in r.stdout


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

    def test_adopt_empty_string_errors(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--adopt '' is a usage error, not a silent fallback to plain rbt-sync."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("fix crash")
        r = git_repo.run_gg("rbt-sync", "-d", "--adopt", "")
        assert r.returncode != 0
        assert "Traceback" not in r.stderr
        assert "branch name" in r.stderr

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


class TestProgress:
    def test_progress_prints_per_action_lines(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--progress emits one prose line per entry: keep/update/discard/create."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")        # will stay KEEP
        git_repo.commit("beta")         # will be amended -> UPDATE
        git_repo.commit("gamma")        # will be dropped -> DISCARD
        _post_series(git_repo)

        # Drop gamma, amend beta, add delta -> series is alpha, beta', delta
        git_repo.git("reset", "--hard", "HEAD~1")           # drop gamma
        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")      # amend beta
        git_repo.commit("delta")                            # new -> create

        r = git_repo.run_gg("rbt-sync", "--progress")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        out = _plain(r.stdout)
        assert "keep (unchanged): alpha" in out, out
        assert re.search(r"posting.*beta", out), out
        assert re.search(r"-> updated r/\d+", out), out
        assert re.search(r"discard r/\d+: gamma", out), out
        assert re.search(r"posting.*delta", out), out
        assert re.search(r"-> created r/\d+", out), out

    def test_no_progress_lines_without_flag(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """A plain real run stays quiet between the plan table and the summary."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")
        _post_series(git_repo)

        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")      # amend beta -> update

        r = git_repo.run_gg("rbt-sync")
        assert r.returncode == 0
        out = _plain(r.stdout)
        # The plan table uses the words keep/update, but never these phrases:
        assert "(unchanged)" not in out
        assert "posting" not in out
        assert "->" not in out

    def test_verbose_implies_progress(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--verbose alone produces the progress one-liners (verbose => progress)."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")
        _post_series(git_repo)

        (git_repo.work_dir / "extra").write_text("changed\n")
        git_repo.git("add", "extra")
        git_repo.git("commit", "--amend", "--no-edit")      # amend beta -> update

        r = git_repo.run_gg("rbt-sync", "-v")
        assert r.returncode == 0
        out = _plain(r.stdout)
        assert re.search(r"posting.*beta", out), out
        assert re.search(r"-> updated r/\d+", out), out

    def test_progress_publish_unchanged_line(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """--progress -p on an unpublished KEEP draft logs publish + result."""
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        _post_series(git_repo)  # gg rbt without -p -> draft, review id r/1000

        r = git_repo.run_gg("rbt-sync", "-p", "--progress")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        out = _plain(r.stdout)
        assert "publish (unchanged): alpha" in out, out
        assert "-> published r/1000" in out, out
        # It is a publish, not a keep-noop or a re-post
        assert "keep (unchanged): alpha" not in out
        assert "posting" not in out


class TestOrphanedReviewIdRepair:
    """A mid-series entry whose stored review_id is empty (its original post
    failed) is auto-re-posted as a fresh review, its successor's dependency is
    refreshed to the new id, and both are published under -p."""

    def test_orphan_repost_fixes_dep_and_publishes(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")    # will be orphaned
        git_repo.commit("gamma")   # successor, keeps its id
        _post_series(git_repo)     # post 3 drafts

        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        assert len(entries) == 3
        beta_old_id = entries[1].review_id
        gamma_id = entries[2].review_id
        assert beta_old_id and gamma_id

        # Corrupt beta: simulate its post having failed (empty review_id),
        # preserving subject + diff_hash so reconcile still matches it as KEEP.
        entries[1] = review_store.ReviewEntry(
            branch="feature", position=2, review_id="",
            subject=entries[1].subject, diff_hash=entries[1].diff_hash,
            published=entries[1].published,
        )
        review_store.save_reviews(entries, cwd=git_repo.work_dir)

        initial = rbt_mock.call_count()
        r = git_repo.run_gg("rbt-sync", "-p", "-U", "reviewer")
        assert r.returncode == 0, f"stderr: {r.stderr}"

        new_calls = rbt_mock.calls()[initial:]
        post_calls = [c for c in new_calls if c and c[0] == "post"]

        # beta is re-created (fresh post, no -r); gamma is dep-updated (re-post -r)
        fresh_posts = [c for c in post_calls if "-r" not in c]
        repost_calls = [c for c in post_calls if "-r" in c]
        assert len(fresh_posts) == 1, post_calls
        assert len(repost_calls) == 1, post_calls

        # gamma re-posted against its own id
        gc = repost_calls[0]
        assert gc[gc.index("-r") + 1] == gamma_id

        # beta now has a new, non-empty id in the DB
        after = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        beta_new_id = after[1].review_id
        assert beta_new_id and beta_new_id != beta_old_id

        # gamma depends on beta's NEW id
        dep_args = [a for a in gc if a.startswith("--depends-on=")]
        assert dep_args, gc
        assert dep_args[0].split("=", 1)[1] == beta_new_id

        # Under -p, publishing happens inline via `rbt post -p` (not a separate
        # `rbt publish` call), so assert the publish flag is present on both posts.
        assert "-p" in fresh_posts[0], fresh_posts[0]
        assert "-p" in repost_calls[0], repost_calls[0]

        # No post used an empty -r id, and no publish used an empty id
        for c in post_calls:
            if "-r" in c:
                assert c[c.index("-r") + 1] != ""
        for c in new_calls:
            if c and c[0] == "publish":
                assert len(c) >= 2 and c[1] != "", c

    def test_orphan_dry_run_plan_shows_repair(
        self, git_repo: GitRepo, rbt_mock: RbtMock,
    ) -> None:
        """-d plan classifies the orphan as update, its successor keep+dep,
        and renders the lost id as r/(lost)."""
        from gg import review_store
        git_repo.create_branch("feature", "master")
        git_repo.commit("alpha")
        git_repo.commit("beta")
        git_repo.commit("gamma")
        _post_series(git_repo)

        entries = review_store.load_reviews("feature", cwd=git_repo.work_dir)
        entries[1] = review_store.ReviewEntry(
            branch="feature", position=2, review_id="",
            subject=entries[1].subject, diff_hash=entries[1].diff_hash,
            published=entries[1].published,
        )
        review_store.save_reviews(entries, cwd=git_repo.work_dir)

        r = git_repo.run_gg("rbt-sync", "-d")
        assert r.returncode == 0
        out = _plain(r.stdout)
        assert "r/(lost)" in out
        beta_line = next(l for l in out.splitlines() if "beta" in l and "r/" in l)
        assert "update" in beta_line
        gamma_line = next(l for l in out.splitlines() if "gamma" in l and "r/" in l)
        assert "keep+dep" in gamma_line


def test_execute_persists_after_each_action(monkeypatch, tmp_path):
    from gg import matcher, sync
    from gg.rbt_post import PostResult

    new = [matcher.NewCommit(rev="aaa", subject="first", diff_hash="h1"),
           matcher.NewCommit(rev="bbb", subject="second", diff_hash="h2")]
    actions = matcher.reconcile([], new)

    posts = iter(["100", "101"])
    monkeypatch.setattr(sync, "post_one",
                        lambda *a, **k: PostResult(review_id=next(posts), output=""))
    monkeypatch.setattr(sync.rb_api, "fetch_reviewers", lambda *a, **k: ([], []))

    snapshots: list[list[str]] = []
    result = sync._execute(
        actions,
        branch_name="feature", tracking="origin/master",
        renumber=False, publish=False, verbose=False, progress=False,
        dry_run=False, explicit_branch=None, initial_depends=None,
        reviewers=["rev"], groups=None, no_numbers=False,
        persist=lambda entries: snapshots.append([e.review_id for e in entries]),
        cwd=tmp_path,
    )
    assert [e.review_id for e in result] == ["100", "101"]
    assert snapshots == [["100"], ["100", "101"]]


def test_preserved_entries_returns_skipped_discards():
    from gg import matcher, sync
    from gg.review_store import ReviewEntry

    kept = ReviewEntry("feature", 3, "900", "old kept", "hk", published=True)
    actions = [
        matcher.SyncAction(kind=matcher.ActionKind.CREATE, old_entry=None,
                           new_commit=matcher.NewCommit("a", "new", "h1"),
                           new_position=1),
        matcher.SyncAction(kind=matcher.ActionKind.SKIP, old_entry=kept,
                           new_commit=None, new_position=None),
    ]
    preserved = sync._preserved_entries(actions, "feature")
    assert [e.review_id for e in preserved] == ["900"]
    assert preserved[0].subject == "old kept"
    assert preserved[0].diff_hash == "hk"
    assert preserved[0].published is True
