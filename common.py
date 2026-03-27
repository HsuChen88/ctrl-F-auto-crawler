"""Shared utilities for Facebook collector modules.

Provides:
- Chrome CDP connection helpers
- Feedback ID encoding / decoding
- GraphQL response parsing
- Comment tree building
- JSONL I/O
- Store Protocol definitions
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

import pychrome

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UNIX_TS_MIN = 946_684_800    # 2000-01-01 00:00:00 UTC
UNIX_TS_MAX = 4_102_444_800  # 2100-01-01 00:00:00 UTC
DAYS_PER_MONTH = 30
DAYS_PER_YEAR = 365

_FOR_LOOP_PREFIX = re.compile(r"^for\s*\(;;\)\s*;\s*")

# ---------------------------------------------------------------------------
# Store Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class StoreProtocol(Protocol):
    """Minimal interface expected by CommentInterceptor."""

    def add_comments(
        self, post_id: str, comments: list[dict], parent_comment_id: str = ""
    ) -> tuple[int, list[dict]]: ...

    def has_post(self, post_id: str) -> bool: ...

    def stats(self) -> tuple[int, int]: ...

    def dump_structural(self) -> list[dict]: ...

    @property
    def intercept_count(self) -> int: ...

    def evict_oldest_posts(self, count: int) -> list[dict]: ...


@runtime_checkable
class MetadataStore(StoreProtocol, Protocol):
    """Extended store that also accepts post-level metadata updates."""

    def update_post_metadata(
        self,
        post_id: str,
        post_text: str = "",
        timestamp: str = "",
        source: str = "graphql",
    ) -> None: ...


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------


def get_tab_url(tab: pychrome.Tab) -> str:
    tab_info = getattr(tab, "_kwargs", {})
    if not isinstance(tab_info, dict):
        return ""
    url = tab_info.get("url", "")
    return url if isinstance(url, str) else ""


def connect_to_chrome(port: int) -> pychrome.Tab:
    browser = pychrome.Browser(url=f"http://127.0.0.1:{port}")
    tabs = browser.list_tab()
    if not tabs:
        print("Error: No tabs found. Open a tab first.", file=sys.stderr)
        sys.exit(1)

    fb_tab = None
    for tab in tabs:
        if "facebook.com" in get_tab_url(tab):
            fb_tab = tab
            break

    if not fb_tab:
        print("Warning: No Facebook tab found. Using first tab.", file=sys.stderr)
        fb_tab = tabs[0]

    fb_tab.start()
    return fb_tab


# ---------------------------------------------------------------------------
# Feedback ID helpers
# ---------------------------------------------------------------------------


def feedback_id(post_id: str, comment_id: str) -> str:
    """Compute base64 feedback_id from post_id and comment_id (same as GraphQL)."""
    if not post_id or not comment_id:
        return ""
    raw = f"feedback:{post_id}_{comment_id}"
    return base64.b64encode(raw.encode()).decode()


def decode_feedback_id(b64: str) -> tuple[str, str]:
    """Decode a base64 feedback ID.

    Returns (post_id, comment_id_or_empty).
    Format after decode: 'feedback:POST_ID' or 'feedback:POST_ID_COMMENT_ID'.
    """
    try:
        decoded = base64.b64decode(b64 + "==").decode("utf-8", errors="ignore")
    except Exception:
        return ("", "")
    m = re.match(r"feedback:(\d+)(?:_(\d+))?", decoded)
    if not m:
        return ("", "")
    return (m.group(1), m.group(2) or "")


# ---------------------------------------------------------------------------
# GraphQL parsing
# ---------------------------------------------------------------------------


def deep_get(obj, *keys, default=None):
    """Safely traverse nested dicts."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return default
    return obj if obj is not None else default


def parse_response_body(raw: str) -> list[dict]:
    """Parse FB GraphQL response body.

    Handles:
      - 'for (;;);' anti-XSSI prefix
      - NDJSON (one JSON object per line)
      - Single JSON object
    """
    raw = _FOR_LOOP_PREFIX.sub("", raw).strip()
    if not raw:
        return []

    lines = raw.split("\n")
    results = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not results:
        try:
            results.append(json.loads(raw))
        except json.JSONDecodeError:
            pass

    return results


# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------


def append_jsonl(path: Path, record: dict):
    """Append a single JSON record to a JSONL file."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"\n  [!] Failed to write to {path}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Comment tree builder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Window / coordinate helpers for auto_clicker
# ---------------------------------------------------------------------------


def get_viewport_to_screen_offset(tab: pychrome.Tab) -> tuple[int, int, float]:
    """Return (offset_x, offset_y, devicePixelRatio) to convert viewport coords to screen coords.

    Uses window.screenX/screenY + outerHeight/innerHeight to compute the
    Chrome UI chrome height (address bar, bookmarks, etc.).
    """
    info = tab.Runtime.evaluate(
        expression="""
        (() => ({
            screenX: window.screenX,
            screenY: window.screenY,
            outerWidth: window.outerWidth,
            outerHeight: window.outerHeight,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            dpr: window.devicePixelRatio,
        }))()
        """,
        returnByValue=True,
    )
    val = info.get("result", {}).get("value", {})
    screen_x = val.get("screenX", 0)
    screen_y = val.get("screenY", 0)
    outer_h = val.get("outerHeight", 0)
    inner_h = val.get("innerHeight", 0)
    dpr = val.get("dpr", 1.0)
    chrome_ui_height = outer_h - inner_h
    return (screen_x, screen_y + chrome_ui_height, dpr)


def viewport_to_screen(
    vp_x: float, vp_y: float, offset: tuple[int, int, float]
) -> tuple[int, int]:
    """Convert viewport-relative coordinates to absolute screen coordinates."""
    ox, oy, dpr = offset
    # pyautogui on Windows with DPI awareness: screen coords are physical pixels
    # if the process is DPI-unaware, we may need to divide by dpr.
    # pyautogui.FAILSAFE coordinates use logical pixels on most setups.
    return (int(ox + vp_x), int(oy + vp_y))


def build_structural_record(
    post_id: str,
    bucket: dict[str, dict],
    parent_map: dict[str, str],
    extra_fields: dict | None = None,
    max_depth: int = 100,
) -> dict:
    """Build a hierarchical comment tree from flat bucket + parent_map.

    Uses backtracking instead of visited.copy() for efficiency.
    Stops recursion beyond max_depth to guard against pathological inputs.
    """
    children_map: dict[str, list[str]] = {}
    root_ids: list[str] = []

    for cid in bucket:
        pid = parent_map.get(cid, "")
        if pid and pid in bucket and pid != cid:
            children_map.setdefault(pid, []).append(cid)
        else:
            root_ids.append(cid)

    def _sort_key(comment_id: str):
        c = bucket.get(comment_id, {})
        created = c.get("created_time")
        created_num = created if isinstance(created, int) else 0
        return (created_num, comment_id)

    visited: set[str] = set()

    def _build_node(comment_id: str, depth: int) -> dict:
        if comment_id in visited:
            return {"comment_id": comment_id, "cycle_detected": True, "replies": []}
        if depth > max_depth:
            return {"comment_id": comment_id, "max_depth_exceeded": True, "replies": []}
        visited.add(comment_id)
        node = dict(bucket[comment_id])
        if not node.get("parent_comment_id"):
            node["parent_comment_id"] = parent_map.get(comment_id, "") or None
        child_ids = sorted(children_map.get(comment_id, []), key=_sort_key)
        node["replies"] = [_build_node(cid, depth + 1) for cid in child_ids]
        visited.discard(comment_id)
        return node

    root_ids_sorted = sorted(root_ids, key=_sort_key)
    record = {
        "post_id": post_id,
        "comments_count": len(bucket),
        "root_comments_count": len(root_ids_sorted),
        "comments": [_build_node(cid, 0) for cid in root_ids_sorted],
    }
    if extra_fields:
        record.update(extra_fields)
    return record
