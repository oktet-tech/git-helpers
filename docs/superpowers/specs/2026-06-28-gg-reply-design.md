# gg reply — post AI.md responses to ReviewBoard

Date: 2026-06-28
Status: Approved (pending spec review)

## Goal

A new `gg reply` subcommand that reads an annotated `gg comments` export (`AI.md`)
and posts the human/AI responses back to ReviewBoard: a threaded reply per
addressed comment plus the corresponding issue-status change. Dry-run by default;
`--post` executes.

## Background

`gg comments` exports open RB issues as markdown grouped by commit/review:

```
## <commit_hash> <summary>  —  r/<review_request_id>
  <review_url>
- <file>:<line> (by <author>): <first line of comment text>
  <reviewer's quoted continuation>
```

The reviewer (or an AI) then annotates each comment in place, under its bullet,
with a workflow marker and prose. Markers seen in practice:

- `[FIXED]` — a fixup commit was made.
- `[ALREADY FIXED]` — resolved earlier on the branch.
- `### ANSWER` — analyzed and declined/disagreed, rationale inline.
- `[DECISION]` — needs a human call.

`gg` currently has no reply capability. ReviewBoard exposes the needed
primitives (confirmed read-only against the live server):

- A review resource has a `replies` link.
- A diff/general comment has an `id`, an `issue_status` field (open by default),
  and an `update` link (PUT to change `issue_status`). The review-request owner
  may set it.
- Replying to a comment = create a reply on the comment's owning review, add a
  diff-/general-comment reply (`reply_to_id`, `text`), then publish (`public=1`).

## Behavior

### Marker → action

| Marker | Reply text | Issue status |
|--------|-----------|--------------|
| `[FIXED]` | posted | resolved |
| `[ALREADY FIXED]` | posted | resolved |
| `### ANSWER` | posted | dropped |
| `[DECISION]` | — | skip (left open) |
| (no marker under a bullet) | — | skip, warned |

If a single comment's response contains both `[FIXED]`/`[ALREADY FIXED]` and
`### ANSWER`, the blocks are concatenated into one reply and "fixed" wins for
status (resolved).

### Reply text

The annotation prose with the marker removed. Removal is marker-shaped:

- A bracket marker at the start of a line (`[FIXED]`, `[ALREADY FIXED]`,
  `[DECISION]`) — drop the bracket token (and following space); keep the rest of
  that line and everything after.
- A heading marker (`### ANSWER`) — drop the whole heading line; keep the prose
  that follows.

Leading/trailing blank lines are trimmed. Example — the AI.md block:

```
[FIXED] fixup 666c64589 -> 0e4b75652. Capture `created` from get_or_create,
count it, and report `Created N bugs (M already existed)`.
```

is posted as (marker word removed, prose kept verbatim, including commit refs):

```
fixup 666c64589 -> 0e4b75652. Capture `created` from get_or_create,
count it, and report `Created N bugs (M already existed)`.
```

### Execution model

- **Default = dry-run.** Print the per-comment plan; change nothing.
- **`--post`** = execute: per owning review, publish one reply containing the
  comment-replies, and set each comment's issue status. Issue-status changes take
  effect immediately on RB (they are not part of a draft); the reply text becomes
  visible on publish.
- `-i/--input <file>` (default `.gg/review-comments.md`), `-b/--branch`.

## AI.md parsing and association

Parsing is **marker-anchored, strict per comment bullet**:

1. Split on `^## ` headers; each header yields the `review_request_id` from
   `r/<id>` and scopes its bullets.
2. Each `^- <file>:<line> (by <author>): <text>` line starts a comment. Its
   "response region" is every line until the next `- ` bullet or `## ` header.
3. Within a response region, the reviewer's quoted continuation precedes the
   first marker; the response is the marker block(s) from the first marker to the
   end of the region. Lines before the first marker are ignored.
4. No marker in the region → the comment has no response → skip (warned). This is
   strict and predictable: e.g. two bullets followed by a single `[FIXED]` means
   only the second bullet (the one the `[FIXED]` sits under) is acted on; the
   first is reported "no response" in the dry-run so the author can add a marker.

Association rule chosen: **strict per-bullet** (not "trailing block covers
preceding bullets") — predictable, and the dry-run makes any gap visible.

## Comment identification

### Primary: embedded id tag (new in `gg comments`)

`gg comments` emits a hidden HTML comment on each bullet carrying the target
coordinates the RB reply API needs:

```
- <file>:<line> (by <author>): <text> <!-- gg <kind> <review_oid> <comment_id> -->
```

- `kind` = `diff` | `general`.
- `review_oid` = id of the review that owns the comment (needed to create the
  reply on that review).
- `comment_id` = the comment to reply to / set issue status on.
- The `review_request_id` comes from the enclosing `## … r/<id>` header.

The tag is invisible in rendered markdown, leaves the human-readable line intact,
and survives annotation (responses are appended below the bullet; the bullet line
is untouched). `gg reply` parses the tag for direct, unambiguous targeting.

### Fallback: content match (for legacy exports)

If a bullet has no `<!-- gg … -->` tag (e.g. an `AI.md` exported before this
change), `gg reply` re-fetches the review-request's open issues and matches by
`(review_request_id, file, first_line, first-line-of-text)`. Exactly one match →
target it; zero or multiple → skip + warn.

## Posting mechanics (RB API)

1. Collect targets: `(review_request_id, review_oid, comment_id, kind, reply_text,
   status)` for every bullet whose marker yields an action.
2. Group targets by `review_oid` (replies attach to a specific review).
3. Per review_oid:
   - Create a reply on `…/reviews/{review_oid}/replies/`.
   - For each target, add a `diff-comments`/`general-comments` reply
     (`reply_to_id=comment_id`, `text=reply_text`).
   - Publish the reply (`public=1`).
4. Per target, set the original comment's `issue_status` (PUT via its update
   link) to `resolved` / `dropped`.

All network writes live in a single module `gg/rb_replies.py` so they can be
stubbed in tests. A per-comment failure is warned and skipped, not fatal.

### Idempotency

Resolving/dropping moves an issue out of "open", so a second run does not re-match
or double-reply `[FIXED]`/`[ALREADY FIXED]`/`### ANSWER` comments; `[DECISION]`
stays skipped. (A reply that posts but whose status PUT then fails could re-post on
re-run — an accepted edge case, warned at the time.)

## Dry-run output

One line per comment plus a summary:

```
r/19051 populate_bugs.py:321   reply + RESOLVE
r/19051 populate_bugs.py:302   SKIP (no response)
r/19060 tools.py:11            SKIP (decision)
r/19072 client.py:53           SKIP (no open issue matches)

3 reply+resolve, 1 reply+drop, 4 skipped
```

## Modules / files (in ~/git-helpers)

- `src/gg/ai_md.py` — pure parser: markdown → list of parsed comments
  (review_request_id, file, line, text, tag {kind,review_oid,comment_id} | None,
  response_text | None, action). No I/O. Unit-tested directly.
- `src/gg/rb_comments.py` — extend `Issue` with `comment_id`, `review_oid`,
  `kind` (already carried by the API payload; just retained).
- `src/gg/comments.py` — emit the `<!-- gg … -->` tag per bullet.
- `src/gg/rb_replies.py` — `post_reply(...)` and `set_issue_status(...)` via
  RBClient; the single mockable network layer for writes.
- `src/gg/reply.py` — the subcommand: parse → identify → build plan → dry-run or
  `--post`.
- `src/gg/cli.py` — register `reply`.
- Git alias `rbt-reply` (user gitconfig) — documented, not code.

## Testing

- `test_ai_md.py` — parser/association: marker→action, strict per-bullet, reply
  text extraction (marker stripped, FIXED+ANSWER combine), tag parsing,
  multi-bullet/single-marker → second only, no-marker → skip.
- `test_reply.py` — plan building and dry-run output from a parsed AI.md against
  a fixed set of fetched issues; `--post` path with a stubbed `rb_replies`
  (asserts the reply/status calls made), no network.
- Comment-id tag emission tested in the existing comments tests via the rbt
  stub seam.

## Non-goals / YAGNI

- No editing/threading beyond a single top-level reply per comment.
- No re-export that preserves prior annotations (the fallback covers the existing
  AI.md; new exports carry tags).
- No "trailing block covers preceding bullets" association.
- No interactive confirmation beyond dry-run → `--post`.

## Open questions

None outstanding.
