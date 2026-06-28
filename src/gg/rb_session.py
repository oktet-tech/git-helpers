"""A process-wide ReviewBoard API session.

By default this reuses a single :class:`RBClient` -- authenticating and
discovering the API root once -- so each request costs a single HTTP round trip
(~0.3s) instead of spawning a fresh ``rbt`` process (~5s of startup) per call.
That turns what used to be minutes of ``gg comments`` into seconds.

Set ``GG_RB_USE_RBT=1`` to force the legacy per-call ``rbt api-get`` transport
instead. This is the fallback used by the test suite (which stubs ``rbt``), and
it also keeps gg working if RBClient construction is ever unavailable.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
from pathlib import Path
from typing import Any

from rbtools.api.client import RBClient
from rbtools.api.errors import APIError

_CONFIG_NAME = ".reviewboardrc"
_client: RBClient | None = None


def _use_rbt_subprocess() -> bool:
    return bool(os.environ.get("GG_RB_USE_RBT"))


def _find_config(start: Path) -> Path | None:
    """Find the nearest .reviewboardrc walking up from `start`, then $HOME."""
    for d in (start, *start.parents):
        candidate = d / _CONFIG_NAME
        if candidate.is_file():
            return candidate
    home = Path.home() / _CONFIG_NAME
    return home if home.is_file() else None


def _load_rb_config(cwd: Path | None) -> tuple[str, str | None]:
    """Return (REVIEWBOARD_URL, API_TOKEN) from the applicable .reviewboardrc."""
    start = Path(cwd or Path.cwd()).resolve()
    cfg_path = _find_config(start)
    if cfg_path is None:
        raise SystemExit("[gg] no .reviewboardrc found (need REVIEWBOARD_URL)")
    # .reviewboardrc is Python (KEY = "value"); exec it in a fresh namespace.
    namespace = runpy.run_path(str(cfg_path))
    url = namespace.get("REVIEWBOARD_URL")
    if not url:
        raise SystemExit(f"[gg] REVIEWBOARD_URL not set in {cfg_path}")
    return url, namespace.get("API_TOKEN")


def get_client(cwd: Path | None = None) -> RBClient:
    """Return the cached RBClient, building it from .reviewboardrc on first use."""
    global _client
    if _client is None:
        url, token = _load_rb_config(cwd)
        # With a token we authenticate non-interactively; without one, RBClient
        # falls back to its cached cookies, matching how `rbt` behaves.
        _client = RBClient(url, api_token=token) if token else RBClient(url)
    return _client


def _api_get_via_rbt(path: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Legacy transport: one `rbt api-get` subprocess per call."""
    r = subprocess.run(
        ["rbt", "api-get", path],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise SystemExit(f"rbt api-get failed for {path}: {msg}")
    return json.loads(r.stdout)


def api_get(path: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """GET an API resource by path (or full URL) and return its raw JSON dict.

    Mirrors ``rbt api-get <path>``: a leading http(s):// is treated as an
    absolute URL (used to follow pagination ``links.next``), otherwise the path
    is resolved against the API root.
    """
    if _use_rbt_subprocess():
        return _api_get_via_rbt(path, cwd=cwd)

    client = get_client(cwd)
    try:
        if path.startswith(("http://", "https://")):
            resource = client.get_url(path)
        else:
            resource = client.get_path(path)
    except APIError as exc:
        raise SystemExit(f"rbt api-get failed for {path}: {exc}") from exc
    return resource.rsp
