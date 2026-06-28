"""The `gg comments` subcommand -- export open RB issues to a file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gg import git, review_store
from gg.rb_comments import Issue, fetch_open_issues


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the comments subcommand."""
    p = subparsers.add_parser(
        "comments", help="export open ReviewBoard issues to a file",
    )
    p.add_argument(
        "-o", "--output", default=".gg/review-comments.md",
        help="output file ('-' for stdout); default .gg/review-comments.md",
    )
    p.add_argument(
        "-b", "--branch", default=None, help="branch (default: current)",
    )
    p.add_argument(
        "-p", "--progress", action="store_true",
        help="print per-review fetch progress to stderr",
    )
    p.set_defaults(func=run)


def _location(issue: Issue) -> str:
    """`path:line` (or `path:first-last`) for a diff comment; '(general)' else."""
    if not issue.file or issue.first_line is None:
        return "(general)"
    if issue.num_lines and issue.num_lines > 1:
        return f"{issue.file}:{issue.first_line}-{issue.first_line + issue.num_lines - 1}"
    return f"{issue.file}:{issue.first_line}"


def _emit_continuation(out: list[str], issue: Issue) -> None:
    """Append the comment's continuation lines (the first is on the bullet)."""
    for line in issue.text.splitlines()[1:]:
        out.append(f"  {line}")


def format_markdown(
    issues: list[Issue],
    *,
    branch: str,
    review_count: int,
    order: list[str],
    meta: dict[str, tuple[str | None, str]],
) -> str:
    """Render open issues grouped by commit/review, in series order.

    Each group header carries the commit hash + summary (and the review link);
    each comment points at a clickable `path:line`. `order` lists review ids in
    series order; `meta` maps review id -> (commit_hash | None, summary).
    """
    by_review: dict[str, list[Issue]] = {}
    for i in issues:
        by_review.setdefault(str(i.review_id), []).append(i)

    out: list[str] = [
        f"# Open review issues — branch {branch} "
        f"({len(issues)} open across {review_count} reviews)",
        "",
    ]

    for rid in order:
        review_issues = by_review.get(rid)
        if not review_issues:
            continue
        commit_hash, summary = meta.get(rid, (None, ""))
        label = f"{commit_hash} {summary}".strip() if commit_hash else summary
        out.append(f"## {label}  —  r/{rid}".rstrip())
        review_url = next((i.review_url for i in review_issues if i.review_url), "")
        if review_url:
            out.append(f"  {review_url}")
        for i in sorted(review_issues, key=lambda i: (i.file or "", i.first_line or 0)):
            first = i.text.splitlines()[0] if i.text else ""
            tag = f" <!-- gg {i.kind} {i.review_oid} {i.comment_id} -->"
            out.append(f"- {_location(i)} (by {i.author}): {first}".rstrip() + tag)
            _emit_continuation(out, i)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _commit_meta(
    entries: list, branch: str, cwd: Path,
) -> tuple[list[str], dict[str, tuple[str | None, str]]]:
    """Build (series-ordered review ids, review_id -> (commit_hash, summary)).

    The commit hash is matched by subject and only attempted for the checked-out
    branch; it degrades to None (summary only) when it cannot be determined.
    """
    hash_by_subject: dict[str, str] = {}
    try:
        if branch == git.branchname(cwd=cwd):
            rng = git.rev_range(cwd=cwd)
            for h, subject in git.revs_with_subjects(rng, cwd=cwd):
                hash_by_subject[review_store.strip_prefix(subject)] = h
    except (OSError, ValueError, RuntimeError):
        hash_by_subject = {}

    ordered = sorted(entries, key=lambda e: e.position)
    order = [e.review_id for e in ordered if e.review_id]
    meta: dict[str, tuple[str | None, str]] = {}
    for e in ordered:
        if not e.review_id:
            continue
        summary = review_store.strip_prefix(e.subject)
        meta[e.review_id] = (hash_by_subject.get(summary), summary)
    return order, meta


def run(args: argparse.Namespace) -> int:
    """Execute the comments subcommand."""
    cwd = Path.cwd()
    branch = args.branch or git.branchname(cwd=cwd)
    entries = review_store.load_reviews(branch, cwd=cwd)
    if not entries:
        print(f"[gg] No reviews for branch '{branch}'.", file=sys.stderr)
        return 1

    all_issues: list[Issue] = []
    read = 0
    skipped = 0
    total = sum(1 for e in entries if e.review_id)
    done = 0
    for e in entries:
        if not e.review_id:
            print(
                f"[gg] skipping entry with no review id: {e.subject}",
                file=sys.stderr,
            )
            continue
        done += 1
        if args.progress:
            print(
                f"[gg] fetching r/{e.review_id} ({done}/{total})...",
                file=sys.stderr,
            )
        try:
            all_issues.extend(fetch_open_issues(e.review_id, cwd=cwd))
            read += 1
        except SystemExit as exc:
            print(f"[gg] skipping r/{e.review_id}: {exc}", file=sys.stderr)
            skipped += 1

    if read == 0 and skipped:
        print(f"[gg] could not read any of {skipped} review(s).", file=sys.stderr)
        return 1

    if not all_issues:
        print("No open issues 🎉")
        return 0

    order, meta = _commit_meta(entries, branch, cwd)
    text = format_markdown(
        all_issues, branch=branch, review_count=read, order=order, meta=meta,
    )

    if args.output == "-":
        sys.stdout.write(text)
    else:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = git.repo_root(cwd=cwd) / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text)
        print(f"Wrote {len(all_issues)} open issue(s) to {out_path}")

    if skipped:
        print(f"[gg] {skipped} review(s) could not be read.", file=sys.stderr)
    return 0
