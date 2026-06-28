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


def _line_label(issue: Issue) -> str:
    """L<first_line> or L<first>-<last> for a diff comment; '' for general."""
    if issue.first_line is None:
        return ""
    if issue.num_lines and issue.num_lines > 1:
        return f"L{issue.first_line}-{issue.first_line + issue.num_lines - 1}"
    return f"L{issue.first_line}"


def _emit_body(out: list[str], issue: Issue) -> None:
    """Append continuation text lines and the review URL for one issue."""
    cont = issue.text.splitlines()[1:]
    for line in cont:
        out.append(f"  {line}")
    if issue.review_url:
        out.append(f"  {issue.review_url}")


def format_markdown(
    issues: list[Issue], *, branch: str, review_count: int,
) -> str:
    """Render open issues as markdown grouped by source file."""
    diff_issues = [i for i in issues if i.kind == "diff"]
    general_issues = [i for i in issues if i.kind == "general"]

    out: list[str] = [
        f"# Open review issues — branch {branch} "
        f"({len(issues)} open across {review_count} reviews)",
        "",
    ]

    by_file: dict[str, list[Issue]] = {}
    for i in diff_issues:
        by_file.setdefault(i.file or "(unknown file)", []).append(i)
    for fname in sorted(by_file):
        out.append(f"## {fname}")
        for i in sorted(by_file[fname], key=lambda i: (i.first_line or 0)):
            first = i.text.splitlines()[0] if i.text else ""
            out.append(f"- {_line_label(i)} (r/{i.review_id}, by {i.author}): {first}".rstrip())
            _emit_body(out, i)
        out.append("")

    if general_issues:
        out.append("## General")
        for i in general_issues:
            first = i.text.splitlines()[0] if i.text else ""
            out.append(f"- (r/{i.review_id}, by {i.author}): {first}".rstrip())
            _emit_body(out, i)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


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

    text = format_markdown(all_issues, branch=branch, review_count=read)

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
