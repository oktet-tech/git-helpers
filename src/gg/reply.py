"""The `gg reply` subcommand -- post AI.md responses to ReviewBoard."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from gg import ai_md, rb_comments, rb_replies
from gg.ai_md import ParsedComment


@dataclass
class PlanItem:
    review_request_id: str
    review_oid: int | None
    comment_id: int | None
    kind: str | None
    file_line: str
    action: str          # resolve | drop | skip-decision | skip-noresponse | skip-nomatch
    text: str | None


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = subparsers.add_parser("reply", help="post AI.md responses to ReviewBoard")
    p.add_argument("-i", "--input", default=".gg/review-comments.md",
                   help="annotated export to read; default .gg/review-comments.md")
    p.add_argument("--post", action="store_true",
                   help="actually publish replies and set issue statuses")
    p.set_defaults(func=run)


def parse_input(text: str) -> list[ParsedComment]:
    return ai_md.parse(text)


def _fetch_open_issues(review_request_id: str, cwd: Path | None):
    return rb_comments.fetch_open_issues(review_request_id, cwd=cwd)


def _match(c: ParsedComment, issues) -> object | None:
    candidates = [
        i for i in issues
        if (i.file or None) == c.file
        and str(i.first_line) == (c.line.split("-")[0] if c.line else "None")
        and i.text.splitlines()[0].strip() == c.text_first_line.strip()
    ]
    return candidates[0] if len(candidates) == 1 else None


def build_plan(comments, *, fetch=_fetch_open_issues, cwd: Path | None = None):
    items: list[PlanItem] = []
    issues_cache: dict[str, list] = {}
    for c in comments:
        loc = f"{c.file}:{c.line}" if c.file else "(general)"
        if c.action.startswith("skip"):
            items.append(PlanItem(c.review_request_id, None, None, None, loc, c.action, None))
            continue
        if c.tag is not None:
            oid, cid, kind = c.tag.review_oid, c.tag.comment_id, c.tag.kind
        elif fetch is not None:
            if c.review_request_id not in issues_cache:
                issues_cache[c.review_request_id] = fetch(c.review_request_id, cwd)
            m = _match(c, issues_cache[c.review_request_id])
            if m is None:
                items.append(PlanItem(c.review_request_id, None, None, None, loc,
                                      "skip-nomatch", None))
                continue
            oid, cid, kind = m.review_oid, m.comment_id, m.kind
        else:
            items.append(PlanItem(c.review_request_id, None, None, None, loc,
                                  "skip-nomatch", None))
            continue
        items.append(PlanItem(c.review_request_id, oid, cid, kind, loc, c.action, c.response))
    return items


_LABEL = {"resolve": "reply + RESOLVE", "drop": "reply + DROP",
          "skip-decision": "SKIP (decision)", "skip-noresponse": "SKIP (no response)",
          "skip-nomatch": "SKIP (no open issue matches)"}


def run_text(text: str, *, post: bool, cwd: Path | None) -> int:
    items = build_plan(parse_input(text), cwd=cwd)
    for it in items:
        print(f"r/{it.review_request_id} {it.file_line:30s} {_LABEL[it.action]}")
    counts = {k: sum(1 for i in items if i.action == k) for k in _LABEL}
    print(f"\n{counts['resolve']} reply+resolve, {counts['drop']} reply+drop, "
          f"{sum(v for k, v in counts.items() if k.startswith('skip'))} skipped",
          file=sys.stderr)
    if not post:
        return 0
    by_review: dict[tuple[str, int], list[PlanItem]] = {}
    for it in items:
        if it.action in ("resolve", "drop"):
            by_review.setdefault((it.review_request_id, it.review_oid), []).append(it)
    for (rr, oid), group in by_review.items():
        targets = [{"kind": i.kind, "comment_id": i.comment_id, "text": i.text}
                   for i in group]
        try:
            rb_replies.post_replies_for_review(rr, oid, targets, cwd=cwd)
            for i in group:
                rb_replies.set_issue_status(rr, oid, i.kind, i.comment_id, i.action, cwd=cwd)
        except SystemExit as exc:
            print(f"[gg] r/{rr} review {oid}: {exc}", file=sys.stderr)
    return 0


def run(args: argparse.Namespace) -> int:
    path = Path(args.input)
    if not path.is_absolute():
        from gg import git
        path = git.repo_root(cwd=Path.cwd()) / path
    if not path.is_file():
        print(f"[gg] input not found: {path}", file=sys.stderr)
        return 1
    return run_text(path.read_text(), post=args.post, cwd=Path.cwd())
