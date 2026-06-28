"""Parse an annotated `gg comments` export into per-comment reply actions."""

from __future__ import annotations

import re
from dataclasses import dataclass

# A `- <file>:<line> (by <author>): <text> [<!-- gg <kind> <oid> <cid> -->]` line.
_BULLET_RE = re.compile(
    r"^- (?P<loc>.+?) \(by (?P<author>.+?)\): (?P<text>.*?)"
    r"(?: <!-- gg (?P<kind>\w+) (?P<oid>\d+) (?P<cid>\d+) -->)?$"
)
_HEADER_RE = re.compile(r"^## .*\br/(?P<rid>\w+)\s*$")
_BRACKET_RE = re.compile(r"^\s*\[(FIXED|ALREADY FIXED|DECISION)\]\s?")
_ANSWER_RE = re.compile(r"^\s*### ANSWER\s*$")


@dataclass(frozen=True)
class Tag:
    kind: str          # "diff" | "general"
    review_oid: int
    comment_id: int


@dataclass
class ParsedComment:
    review_request_id: str
    file: str | None
    line: str | None        # "10" or "10-13" as written; None for general
    author: str
    text_first_line: str
    tag: Tag | None
    comment_id: int | None  # convenience: tag.comment_id or None
    response: str | None    # reply text (markers removed) or None
    action: str             # resolve | drop | skip-decision | skip-noresponse


def _split_location(loc: str) -> tuple[str | None, str | None]:
    if loc == "(general)":
        return None, None
    file, _, line = loc.rpartition(":")
    return (file or None), (line or None)


def _markers_in(region: list[str]) -> list[str]:
    out: list[str] = []
    for ln in region:
        m = _BRACKET_RE.match(ln)
        if m:
            out.append(m.group(1))
        elif _ANSWER_RE.match(ln):
            out.append("ANSWER")
    return out


def _first_marker_index(region: list[str]) -> int | None:
    for i, ln in enumerate(region):
        if _BRACKET_RE.match(ln) or _ANSWER_RE.match(ln):
            return i
    return None


def _action_for(markers: list[str]) -> str:
    if "FIXED" in markers or "ALREADY FIXED" in markers:
        return "resolve"
    if "ANSWER" in markers:
        return "drop"
    if "DECISION" in markers:
        return "skip-decision"
    return "skip-noresponse"


def _response_text(region: list[str], start: int) -> str:
    lines: list[str] = []
    for ln in region[start:]:
        if _ANSWER_RE.match(ln):
            continue  # drop the heading line entirely
        m = _BRACKET_RE.match(ln)
        if m:
            lines.append(ln[m.end():])
        elif ln.startswith("  "):
            lines.append(ln[2:])
        else:
            lines.append(ln.strip())
    return "\n".join(lines).strip()


def parse(text: str) -> list[ParsedComment]:
    """Parse the annotated export into ParsedComment objects, in document order."""
    lines = text.splitlines()
    comments: list[ParsedComment] = []
    rid: str | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        h = _HEADER_RE.match(line)
        if h:
            rid = h.group("rid")
            i += 1
            continue
        b = _BULLET_RE.match(line)
        if b and rid is not None:
            j = i + 1
            region: list[str] = []
            while j < n and not _BULLET_RE.match(lines[j]) and not _HEADER_RE.match(lines[j]):
                region.append(lines[j])
                j += 1
            comments.append(_build(rid, b, region))
            i = j
            continue
        i += 1
    return comments


def _build(rid: str, b: "re.Match[str]", region: list[str]) -> ParsedComment:
    file, line = _split_location(b.group("loc"))
    tag = None
    if b.group("kind"):
        tag = Tag(kind=b.group("kind"), review_oid=int(b.group("oid")),
                  comment_id=int(b.group("cid")))
    start = _first_marker_index(region)
    if start is None:
        action, response = "skip-noresponse", None
    else:
        markers = _markers_in(region[start:])
        action = _action_for(markers)
        response = None if action.startswith("skip") else _response_text(region, start)
    return ParsedComment(
        review_request_id=rid, file=file, line=line, author=b.group("author"),
        text_first_line=b.group("text"), tag=tag,
        comment_id=(tag.comment_id if tag else None),
        response=response, action=action,
    )
