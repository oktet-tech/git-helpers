"""The `gg rbt-sync` subcommand -- reconcile commit series with ReviewBoard."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from gg import diff_cache, git, rb_api, review_store
from gg.matcher import ActionKind, NewCommit, SyncAction, reconcile
from gg.numbering import assign_numbers
from gg.rbt_close import close_discarded, close_submitted
from gg.rbt_post import post_one
from gg.rbt_publish import publish_one
from gg.sync_edit import edit_plan
from gg.sync_plan import format_plan

_BOLD = "\033[1m"
_RESET = "\033[0m"


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the rbt-sync subcommand."""
    p = subparsers.add_parser("rbt-sync", help="reconcile commit series with ReviewBoard")
    p.add_argument("-d", "--dry", action="store_true", help="plan only, don't execute")
    p.add_argument("-i", "--interactive", action="store_true", help="edit plan before executing")
    p.add_argument("-f", "--force", action="store_true",
                   help="re-post every matched commit, ignoring diff hash")
    p.add_argument("--renumber", action="store_true", help="full renumber instead of fractional")
    p.add_argument("-p", "--publish", action="store_true", help="publish new/updated requests")
    p.add_argument("-v", "--verbose", action="store_true", help="show rbt output")
    p.add_argument("--progress", action="store_true", help="print one line per action")
    p.add_argument(
        "-D", "--depends-on", default=None, metavar="ID",
        help="first patch depends on this review request ID",
    )
    p.add_argument("-U", "--users", action="append", default=[], help="reviewer (--target-people)")
    p.add_argument("-G", "--groups", action="append", default=[], help="review group (--target-groups)")
    p.add_argument("-n", "--no-numbers", action="store_true", help="don't number the patches")
    p.add_argument("-b", "--branch", default=None, help="explicit --branch for new reviews")
    p.add_argument("--new", action="store_true",
                   help="forget old reviews, post current commits as a fresh series")
    p.add_argument("--close", action="store_true",
                   help="close all reviews as submitted and clear DB")
    p.add_argument(
        "--adopt", metavar="SRC", default=None,
        help="reconcile against SRC branch's reviews; save under current branch",
    )
    p.add_argument(
        "--adopt-overwrite", action="store_true",
        help="(with --adopt) overwrite current branch's existing rows",
    )
    p.add_argument(
        "--upstream", default=None, metavar="REF",
        help="override @{u} for both the diff base and rbt's --tracking-branch",
    )
    p.add_argument("range", nargs="?", default=None, help="revision range (default: tracking..HEAD)")
    p.set_defaults(func=run)


def _number_matches(old_entry: review_store.ReviewEntry, num_str: str, old_total: int) -> bool:
    """True when the old review already has the correct number prefix."""
    old_num_str = f"[{old_entry.position}/{old_total}]"
    return old_num_str == num_str


def _build_new_commits(revs: list[str], *, cwd: Path) -> list[NewCommit]:
    """Build NewCommit list from revision hashes."""
    commits = []
    for rev in revs:
        subject = git.summary(rev, cwd=cwd)
        h = diff_cache.diff_hash(rev, cwd=cwd)
        commits.append(NewCommit(rev=rev, subject=subject, diff_hash=h))
    return commits


def _execute(
    actions: list[SyncAction],
    *,
    branch_name: str,
    tracking: str,
    renumber: bool,
    publish: bool,
    verbose: bool,
    progress: bool,
    dry_run: bool,
    explicit_branch: str | None,
    initial_depends: str | None,
    reviewers: list[str] | None = None,
    groups: list[str] | None = None,
    no_numbers: bool = False,
    persist: Callable[[list[review_store.ReviewEntry]], None] | None = None,
    cwd: Path,
) -> list[review_store.ReviewEntry]:
    """Execute sync actions and return updated review entries.

    On partial failure, returns entries for completed actions.
    """
    numbered = assign_numbers(actions, renumber=renumber)
    old_total = sum(1 for a, _ in numbered if a.old_entry is not None)

    # Phase 1: discard removed reviews
    for action, _ in numbered:
        if action.kind == ActionKind.DISCARD and action.old_entry:
            if progress:
                print(
                    f"{_BOLD}discard r/{action.old_entry.review_id}: "
                    f"{action.old_entry.subject}{_RESET}",
                    flush=True,
                )
            close_discarded(
                action.old_entry.review_id,
                dry_run=dry_run, verbose=verbose, cwd=cwd,
            )

    # Phase 2: process non-discard actions in order
    entries: list[review_store.ReviewEntry] = []
    prev_review_id = initial_depends

    for action, num_str in numbered:
        if action.kind == ActionKind.DISCARD:
            continue
        if action.kind == ActionKind.SKIP:
            if progress:
                subj = (
                    action.new_commit.subject if action.new_commit
                    else action.old_entry.subject if action.old_entry
                    else ""
                )
                print(f"{_BOLD}skip: {subj}{_RESET}", flush=True)
            continue

        assert action.new_commit is not None
        if no_numbers:
            num_prefix = ""
        else:
            num_prefix = f"{num_str}: " if num_str != "--" else ""

        if (action.kind == ActionKind.KEEP
                and not action.needs_dep_update
                and (not renumber
                     or _number_matches(action.old_entry, num_str, old_total))):
            # Nothing about the commit changed. With --publish, publish the
            # review only if it is still an unpublished draft; an already
            # published review is left untouched.
            assert action.old_entry is not None
            entry_published = bool(action.old_entry.published)
            if publish and not entry_published:
                if progress:
                    print(
                        f"{_BOLD}publish (unchanged): "
                        f"{action.new_commit.subject}{_RESET}",
                        flush=True,
                    )
                rc = publish_one(
                    action.old_entry.review_id,
                    dry_run=dry_run, verbose=verbose, cwd=cwd,
                )
                if rc == 0:
                    entry_published = True
                    if progress:
                        print(
                            f"{_BOLD}  -> published "
                            f"r/{action.old_entry.review_id}{_RESET}",
                            flush=True,
                        )
            elif progress:
                print(
                    f"{_BOLD}keep (unchanged): {action.new_commit.subject}{_RESET}",
                    flush=True,
                )
            entries.append(review_store.ReviewEntry(
                branch=branch_name,
                position=len(entries) + 1,
                review_id=action.old_entry.review_id,
                subject=review_store.strip_prefix(action.new_commit.subject),
                diff_hash=action.new_commit.diff_hash,
                published=entry_published,
            ))
            if persist:
                persist(entries)
            prev_review_id = action.old_entry.review_id
            continue

        # UPDATE / KEEP_DEP entries with an empty review_id are recovery cases
        # (a previous post failed mid-flight). Without a review_id there is no
        # `rbt post -r ID`; the call is effectively a fresh post, so it both
        # accepts and (with --publish) requires reviewers.
        needs_fresh_post = (
            action.kind == ActionKind.CREATE
            or (action.old_entry is not None and not action.old_entry.review_id)
        )

        if progress:
            pos = f" {num_str}" if num_str != "--" else ""
            print(
                f"{_BOLD}posting{pos}: {action.new_commit.subject} ...{_RESET}",
                flush=True,
            )

        if needs_fresh_post:
            if reviewers is not None or groups is not None:
                create_reviewers = reviewers or []
                create_groups = groups or []
            elif prev_review_id:
                create_reviewers, create_groups = rb_api.fetch_reviewers(
                    prev_review_id, cwd=cwd,
                )
            else:
                create_reviewers, create_groups = [], []
            result = post_one(
                action.new_commit.rev, tracking,
                first_post=True,
                publish=publish,
                dry_run=dry_run,
                verbose=verbose,
                reviewers=create_reviewers,
                groups=create_groups,
                explicit_branch=explicit_branch,
                num_string=num_prefix,
                depends_on=prev_review_id,
                cwd=cwd,
            )
            rid = result.review_id
        else:
            # UPDATE or KEEP_DEP with a real review_id: re-post with -r ID
            assert action.old_entry is not None
            result = post_one(
                action.new_commit.rev, tracking,
                review_id=action.old_entry.review_id,
                publish=publish,
                dry_run=dry_run,
                verbose=verbose,
                explicit_branch=explicit_branch,
                num_string=num_prefix,
                depends_on=prev_review_id,
                cwd=cwd,
            )
            rid = result.review_id or action.old_entry.review_id

        if progress and rid:
            verb = "created" if needs_fresh_post else "updated"
            print(f"{_BOLD}  -> {verb} r/{rid}{_RESET}", flush=True)

        entries.append(review_store.ReviewEntry(
            branch=branch_name,
            position=len(entries) + 1,
            review_id=rid or "",
            subject=review_store.strip_prefix(action.new_commit.subject),
            diff_hash=action.new_commit.diff_hash,
            published=bool(publish),
        ))
        if persist:
            persist(entries)
        prev_review_id = rid

    return entries


def _format_summary(actions: list[SyncAction]) -> str:
    """One-line summary of sync action counts."""
    counts: dict[str, int] = {}
    for a in actions:
        if a.kind in (ActionKind.KEEP, ActionKind.KEEP_DEP):
            counts["kept"] = counts.get("kept", 0) + 1
        elif a.kind == ActionKind.UPDATE:
            counts["updated"] = counts.get("updated", 0) + 1
        elif a.kind == ActionKind.CREATE:
            counts["created"] = counts.get("created", 0) + 1
        elif a.kind == ActionKind.DISCARD:
            counts["discarded"] = counts.get("discarded", 0) + 1
        elif a.kind == ActionKind.SKIP:
            counts["skipped"] = counts.get("skipped", 0) + 1
    parts = []
    for key in ("kept", "updated", "created", "discarded", "skipped"):
        if key in counts:
            parts.append(f"{counts[key]} {key}")
    return "Synced: " + ", ".join(parts)


def _preserved_entries(
    actions: list[SyncAction], branch_name: str,
) -> list[review_store.ReviewEntry]:
    """Skipped-discard rows (kept, not discarded) to persist alongside actions.

    Positions are placeholders (0); the persist closure reassigns them after the
    action entries.
    """
    out: list[review_store.ReviewEntry] = []
    for a in actions:
        if a.kind == ActionKind.SKIP and a.old_entry and not a.new_commit:
            out.append(review_store.ReviewEntry(
                branch=branch_name,
                position=0,
                review_id=a.old_entry.review_id,
                subject=a.old_entry.subject,
                diff_hash=a.old_entry.diff_hash,
                published=bool(a.old_entry.published),
            ))
    return out


def run(args: argparse.Namespace) -> int:
    """Execute the rbt-sync subcommand."""
    cwd = Path.cwd()
    branch_name = git.branchname(cwd=cwd)

    if args.adopt is not None and not args.adopt.strip():
        print("[gg] --adopt requires a branch name", file=sys.stderr)
        return 1
    if args.adopt_overwrite and not args.adopt:
        print("[gg] --adopt-overwrite requires --adopt", file=sys.stderr)
        return 1
    if args.adopt and (args.new or args.close):
        print("[gg] --adopt is incompatible with --new/--close", file=sys.stderr)
        return 1
    if args.adopt == branch_name:
        print(
            f"[gg] cannot adopt from current branch '{branch_name}'",
            file=sys.stderr,
        )
        return 1

    if args.close:
        old = review_store.load_reviews(branch_name, cwd=cwd)
        if not old:
            print("No reviews to close.")
            return 1
        for entry in old:
            print(f"  close r/{entry.review_id}  {entry.subject}")
        if args.dry:
            return 0
        for entry in old:
            close_submitted(entry.review_id, verbose=args.verbose, cwd=cwd)
        review_store.clear_branch(branch_name, cwd=cwd)
        print(f"Closed {len(old)} review(s) as submitted.", file=sys.stderr)
        return 0

    try:
        tracking = git.tracking_branch(cwd=cwd, override=args.upstream)
        range_spec = args.range or git.rev_range(cwd=cwd, override=args.upstream)
    except git.NoUpstreamError as e:
        print(
            f"[gg] {e}. Set one with `git branch --set-upstream-to=<ref>` "
            f"or pass `--upstream <ref>`.",
            file=sys.stderr,
        )
        return 1
    revs = git.list_revs(range_spec, cwd=cwd)

    if not revs:
        print("No commits in range.")
        return 1

    source_branch = args.adopt or branch_name
    old = review_store.load_reviews(source_branch, cwd=cwd)
    if args.adopt and not old:
        print(
            f"[gg] no reviews to adopt from branch '{args.adopt}'",
            file=sys.stderr,
        )
        return 1
    if args.adopt:
        existing = review_store.load_reviews(branch_name, cwd=cwd)
        if existing and not args.adopt_overwrite:
            print(
                f"[gg] branch '{branch_name}' already has {len(existing)} reviews; "
                f"pass --adopt-overwrite to replace",
                file=sys.stderr,
            )
            return 1
    if not args.adopt and not old and not args.new:
        print(
            "[gg] No existing reviews; posting as a fresh series.",
            file=sys.stderr,
        )
        args.new = True

    new = _build_new_commits(revs, cwd=cwd)
    actions = reconcile([] if args.new else old, new)

    if args.interactive:
        edited = edit_plan(actions, renumber=args.renumber)
        if edited is None:
            print("Aborted.")
            return 0
        actions = edited

    # --force: re-post every matched commit, ignoring diff hash.
    # Runs AFTER the interactive editor so the user can still skip
    # individual entries from the plan.
    if args.force:
        for a in actions:
            if a.kind in (ActionKind.KEEP, ActionKind.KEEP_DEP):
                a.kind = ActionKind.UPDATE
                a.needs_dep_update = False

    # Show plan
    plan = format_plan(
        actions, renumber=args.renumber, publish=args.publish,
        reviewers=args.users, groups=args.groups,
        force=args.force,
    )
    print(plan)

    if args.dry:
        return 0

    print()
    preserved = _preserved_entries(actions, branch_name)

    def _persist(action_entries: list[review_store.ReviewEntry]) -> None:
        merged = list(action_entries)
        for p in preserved:
            merged.append(replace(p, position=len(merged) + 1))
        review_store.save_reviews(merged, cwd=cwd)
        diff_cache.save_hashes(
            {e.diff_hash for e in merged}, cwd=cwd, branch=branch_name,
        )

    entries = _execute(
        actions,
        branch_name=branch_name,
        tracking=tracking,
        renumber=args.renumber,
        publish=args.publish,
        verbose=args.verbose,
        progress=args.progress or args.verbose,
        dry_run=False,
        explicit_branch=args.branch,
        initial_depends=args.depends_on,
        reviewers=args.users or None,
        groups=args.groups or None,
        no_numbers=args.no_numbers,
        persist=_persist,
        cwd=cwd,
    )

    print(_format_summary(actions), file=sys.stderr)

    # Final backstop: the in-loop persist already wrote completed actions; this
    # also covers an all-skipped run where the loop never persisted. save_reviews
    # returns early on an empty list, matching the previous `if entries:` guard.
    _persist(entries)

    return 0
