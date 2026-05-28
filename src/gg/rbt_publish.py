"""Publish ReviewBoard review-request drafts via `rbt publish`."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from gg.rbt_retry import run_with_retry

# RB reports a publish against a request with no pending draft as
# API Error 100 ("Does Not Exist"), wrapped as "may already be published".
_ALREADY_PUBLISHED_RE = re.compile(
    r"API Error 100|may already be published",
    re.IGNORECASE,
)


def publish_one(
    review_id: str,
    *,
    dry_run: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
) -> int:
    """Publish a single review request draft. Returns rbt's exit code.

    Treats RB's "already published / no draft" (API Error 100) as a
    successful no-op so re-running publish over public reviews is benign.
    """
    cmd = ["rbt", "publish", review_id]

    if dry_run:
        print(shlex.join(cmd))
        return 0

    r = run_with_retry(cmd, cwd=cwd)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        if _ALREADY_PUBLISHED_RE.search(out):
            sys.stderr.write(
                f"[gg] r/{review_id} already published (nothing to publish)\n"
            )
            return 0
        sys.stderr.write(f"\n[gg] rbt publish r/{review_id} failed (exit {r.returncode})\n")
        sys.stderr.write(out)
        return r.returncode
    if verbose:
        out = r.stdout + r.stderr
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
    return 0
