# Export open ReviewBoard issues to a file (`gg comments`)

## Problem

After a series is reviewed, reviewers leave open issues spread across many
ReviewBoard review requests (one per commit in the series). To address them
with an LLM, the author wants every open issue collected into a single file
they can hand to Claude. There is no command for this today — `rb_api.py`
fetches review *requests* (summary, blocks, reviewers) but not the comments
on them.

## Goal

A new read-only `gg` subcommand that walks the current branch's review
series (the ids in `reviews.db`), collects every **open issue** comment, and
writes them to one markdown file grouped by source file — suitable for
feeding to an LLM that has the repo checked out.

## Decisions

- **Backend:** ReviewBoard only. The branch's review ids come from
  `review_store.load_reviews(branch)`; comments are read via `rbt api-get`.
- **Scope:** open issues only — a comment with `issue_opened == true` and
  `issue_status == "open"`. Resolved/dropped issues and plain (non-issue)
  comments are excluded.
- **Format:** markdown grouped by source file (general/review-wide comments
  in their own section).
- **Command name:** `gg comments`, with a git alias `git rbt-comments`.
- **Default output:** write `.gg/review-comments.md` (the `.gg/` dir is
  already gitignored) and print its path; `-o FILE` overrides; `-o -` writes
  to stdout.

## Command

```
git rbt-comments              # write .gg/review-comments.md, print its path
git rbt-comments -o FILE      # write to FILE instead
git rbt-comments -o -         # write to stdout (for piping)
git rbt-comments -b BRANCH    # use BRANCH instead of the current branch
```

Read-only; no remote mutations. No `-d/--dry` (nothing to dry-run).

## Architecture — two new modules

The existing repo splits the RB API shim (`rb_api.py`) from the
subcommands. This feature follows the same split.

### `src/gg/rb_comments.py` — fetch layer

One public function and one dataclass:

```python
@dataclass
class Issue:
    review_id: str
    review_url: str            # the review request's absolute_url
    file: str | None           # dest_file for diff comments; None for general
    first_line: int | None     # None for general comments
    num_lines: int | None      # None for general comments
    text: str
    author: str                # username of the reviewer who wrote the comment
    kind: str                  # "diff" | "general"

def fetch_open_issues(review_id: str, *, cwd: Path | None = None) -> list[Issue]:
    ...
```

`fetch_open_issues` walks the RB Web API via `rbt api-get`:

- `…/review-requests/{id}/reviews/` → each published review. The review's
  `links.user.title` is the comment author for that review's comments.
- `…/review-requests/{id}/reviews/{rid}/diff-comments/?expand=filediff` →
  diff comments with the filediff inlined, so the filename (`dest_file`) is
  available without an extra call per comment.
- `…/review-requests/{id}/reviews/{rid}/general-comments/` → review-wide
  comments.
- Keep only comments where `issue_opened` is true and `issue_status ==
  "open"`.

The review request's web link is its `absolute_url`. `rb_api.fetch_review`
is extended to also return `absolute_url` so it (and `fetch_open_issues`)
can surface it; existing callers ignore the added key.

A thin internal `_api_get(path, *, cwd) -> dict` helper wraps the
`rbt api-get` + JSON-parse + error path (mirroring `rb_api`), giving tests a
single seam and keeping each resource fetch one line.

### `src/gg/comments.py` — the `gg comments` subcommand

`add_parser(subparsers)` registers `comments` with `-o/--output` (default
`.gg/review-comments.md`, `-` for stdout) and `-b/--branch` (default current
branch). `run(args)`:

1. Resolve the branch and `review_store.load_reviews(branch)`.
2. For each entry with a non-empty `review_id`, call `fetch_open_issues`;
   collect into one list. On a per-review fetch error, warn to stderr and
   continue (count the skips).
3. Format markdown and write to the output destination; print the path
   (unless stdout).

Registered in `cli.py` next to the other `add_parser` calls. The
`git rbt-comments` alias is added to `gitconfig.go` alongside `git rbt`.

## Data flow

`reviews.db (branch → r/ids)` → for each id `fetch_open_issues` → flat
`list[Issue]` → group (files alphabetical, sorted by `first_line`; general
section last; reviews in series order within ties) → markdown → output file
(+ printed path).

## Output format

```
# Open review issues — branch <branch> (<N> open across <M> reviews)

## src/gg/sync.py
- L142 (r/19052, by ark-oleg): prefer raising CommandError here
  https://rb.example/r/19052/
- L160-163 (r/19052, by ark-alxk): dedupe this block
  https://rb.example/r/19052/

## General
- (r/19057, by ark-oleg): add a module docstring
  https://rb.example/r/19057/
```

- A single-line diff comment renders `L<first_line>`; a span renders
  `L<first_line>-<first_line + num_lines - 1>`.
- Multi-line comment text: the first line follows the bullet; continuation
  lines are indented two spaces under the bullet.
- The review URL is printed on its own indented line under each bullet.

## Error handling

- No `reviews.db` or no reviews for the branch → friendly message to stderr,
  exit 1 (same style as `rbt-sync`'s "no reviews" paths).
- A single review's `api-get` fails → warn to stderr, skip it, continue; the
  header's "across M reviews" reflects only the reviews successfully read,
  and a trailing stderr note reports how many were skipped.
- Zero open issues across all reviews → write/print `No open issues 🎉` and
  exit 0.
- Entries whose stored `review_id` is empty (orphaned, see the separate
  auto-repair work) are skipped with a one-line stderr note.

## Non-goals

- No GitHub PR support (RB only).
- No resolved/dropped/plain-comment export, no filtering flags beyond the
  fixed "open issues" scope (YAGNI; can be added later).
- No mutation of issue status (this only reads).
- No fetching of the surrounding source lines from RB — the consuming LLM
  has the repo and can open `file:line` itself.

## Testing

The main lift is extending the `rbt_mock` fixture in `tests/conftest.py` to
serve the comment resources from seeded fixtures:

- `GET …/review-requests/{id}/reviews/` → `{"reviews": [...]}`
- `GET …/reviews/{rid}/diff-comments/` (with `?expand=filediff`) →
  `{"diff_comments": [...]}` including an inlined `filediff` with
  `dest_file`
- `GET …/reviews/{rid}/general-comments/` → `{"general_comments": [...]}`

plus a fixture helper to attach comments (with `issue_opened`/`issue_status`,
`text`, `first_line`/`num_lines`, author) to a review id. Then integration
tests through the CLI (`python -m gg comments`), matching repo convention:

- open issues are collected; resolved, dropped, and non-issue comments are
  excluded;
- output groups diff comments by file and puts general comments in their own
  section; single-line vs line-span rendering is correct;
- comments from multiple reviews in the series are aggregated;
- no-reviews → friendly error, exit 1;
- zero open issues → "No open issues 🎉", exit 0;
- `-o -` writes to stdout; the default writes `.gg/review-comments.md` and
  prints its path;
- a failing review fetch → warning on stderr, the run still exports the
  other reviews' issues.

Unit tests for `rb_comments.fetch_open_issues` use the same mock (via the CLI
seam) or monkeypatch `_api_get` to return canned resource dicts.
