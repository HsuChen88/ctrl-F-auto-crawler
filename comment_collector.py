#!/usr/bin/env python3
"""
Facebook Comment Collector — Passive Network Intercept Mode.

Listens to Chrome CDP Network events to capture GraphQL responses
related to comments (CommentsListComponents / Depth1 / Depth2).
The script NEVER sends requests, clicks, scrolls, or injects scripts.
All interaction is done by YOU in the browser.

Usage:
  1. ./start_chrome.sh
  2. Log into Facebook, navigate to a post with comments.
  3. uv run python comment_collector.py
  4. Manually click "View more comments" / "View replies" in the browser.
  5. Press Ctrl+C when done. Results are saved to JSON + raw JSONL.
"""

import argparse
import json
import re
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote_plus

import pychrome

from common import (
    DAYS_PER_MONTH,
    DAYS_PER_YEAR,
    UNIX_TS_MAX,
    UNIX_TS_MIN,
    MetadataStore,
    StoreProtocol,
    append_jsonl,
    build_structural_record,
    connect_to_chrome,
    decode_feedback_id,
    deep_get,
    get_tab_url,
    parse_response_body,
)

COMMENTS_QUERIES = {
    "CommentListComponentsRootQuery",
    "CommentsListComponentsPaginationQuery",
    "Depth1CommentsListPaginationQuery",
    "Depth2CommentsListPaginationQuery",
}

FOCUSED_STORY_QUERY = "CometFocusedStoryViewUFIQuery"

KNOWN_DOC_IDS = {
    "26150828281212332": "CommentListComponentsRootQuery",
    "26619250424347780": "CommentsListComponentsPaginationQuery",
    "26276906848640473": "Depth1CommentsListPaginationQuery",
    "26902344142700705": "CometFocusedStoryViewUFIQuery",
}

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

_RELATIVE_TIME_RE = re.compile(
    r"^\s*(\d+)\s*(秒|分鐘|小時|天|週|周|月|年|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|sec|min|hr|wk|mo|yr)\s*(前|ago)?\s*$",
    re.IGNORECASE,
)

PENDING_TTL_SECONDS = 60


def _format_date_zh(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _normalize_unix_timestamp(value) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    ts = float(value)
    if ts > 1_000_000_000_000:
        ts = ts / 1000.0
    if ts < UNIX_TS_MIN or ts > UNIX_TS_MAX:
        return None
    return int(ts)


def normalize_post_timestamp(value: str, now: datetime | None = None) -> str:
    """Normalize relative FB timestamps to absolute date (YYYY年M月D日)."""
    text = (value or "").strip()
    if not text:
        return ""

    lower = text.lower()
    if lower in {"just now", "剛剛"}:
        base = now or datetime.now()
        return _format_date_zh(base)

    match = _RELATIVE_TIME_RE.match(text)
    if not match:
        return text

    amount = int(match.group(1))
    unit = match.group(2).lower()
    base = now or datetime.now()

    if unit in {"秒", "second", "seconds", "sec", "secs"}:
        dt = base - timedelta(seconds=amount)
    elif unit in {"分鐘", "minute", "minutes", "min", "mins"}:
        dt = base - timedelta(minutes=amount)
    elif unit in {"小時", "hour", "hours", "hr", "hrs"}:
        dt = base - timedelta(hours=amount)
    elif unit in {"天", "day", "days"}:
        dt = base - timedelta(days=amount)
    elif unit in {"週", "周", "week", "weeks", "wk"}:
        dt = base - timedelta(weeks=amount)
    elif unit in {"月", "month", "months", "mo"}:
        dt = base - timedelta(days=amount * DAYS_PER_MONTH)
    elif unit in {"年", "year", "years", "yr"}:
        dt = base - timedelta(days=amount * DAYS_PER_YEAR)
    else:
        return text
    return _format_date_zh(dt)


def extract_post_context_from_focused_story(parsed_objects: list[dict]) -> dict:
    """Extract post-level context (timestamp/post_text) from focused-story response."""
    creation_candidates: list[int] = []
    text_candidates: list[str] = []

    def _walk(obj):
        if isinstance(obj, dict):
            ts = _normalize_unix_timestamp(obj.get("creation_time"))
            if ts is not None:
                creation_candidates.append(ts)

            for key in ("message", "title", "body", "content"):
                val = obj.get(key)
                if isinstance(val, dict):
                    t = (val.get("text") or "").strip()
                    if t:
                        text_candidates.append(t)
                elif isinstance(val, str):
                    t = val.strip()
                    if t:
                        text_candidates.append(t)

            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    for top in parsed_objects:
        _walk(top)

    context = {"post_text": "", "timestamp": ""}
    if creation_candidates:
        context["timestamp"] = _format_date_zh(datetime.fromtimestamp(min(creation_candidates)))
    if text_candidates:
        context["post_text"] = max(text_candidates, key=len)
    return context


def extract_request_meta(post_data: str) -> dict:
    """Extract fb_api_req_friendly_name, doc_id, and variables from POST body."""
    if not post_data:
        return {}
    meta = {}
    for part in post_data.split("&"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        key = unquote_plus(key)
        val = unquote_plus(val)
        if key == "fb_api_req_friendly_name":
            meta["friendly_name"] = val
        elif key == "doc_id":
            meta["doc_id"] = val
        elif key == "variables":
            try:
                meta["variables"] = json.loads(val)
            except json.JSONDecodeError:
                meta["variables_raw"] = val
    return meta


def identify_query(meta: dict) -> str | None:
    """Return the canonical query name if this is a comment-related request."""
    name = meta.get("friendly_name", "")
    if name in COMMENTS_QUERIES or name == FOCUSED_STORY_QUERY:
        return name
    doc_id = meta.get("doc_id", "")
    if doc_id in KNOWN_DOC_IDS:
        return KNOWN_DOC_IDS[doc_id]
    return None


def extract_comment_node(node: dict) -> dict | None:
    """Extract a single comment from a GraphQL edge node."""
    if not isinstance(node, dict):
        return None
    body_text = deep_get(node, "body", "text", default="")
    author_name = deep_get(node, "author", "name", default="")
    created_time = node.get("created_time")
    created_time_norm = _normalize_unix_timestamp(created_time)
    comment_id = node.get("legacy_fbid") or node.get("id", "")
    fb_id = deep_get(node, "feedback", "id", default="")

    if not body_text and not author_name:
        return None

    return {
        "comment_id": str(comment_id),
        "feedback_id": fb_id,
        "author": author_name,
        "body": body_text,
        "created_time": created_time,
        "time": _format_date_zh(datetime.fromtimestamp(created_time_norm)) if created_time_norm else "",
    }


def extract_comments_from_response(parsed_objects: list[dict]) -> list[dict]:
    """Walk parsed response objects and extract comment nodes.

    FB responses can be NDJSON with multiple top-level objects.
    Comments live under various paths — we search recursively for
    edges arrays containing comment-shaped nodes.
    """
    comments = []

    def _walk_edges(edges):
        if not isinstance(edges, list):
            return
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if node:
                c = extract_comment_node(node)
                if c:
                    comments.append(c)

    def _search(obj):
        if isinstance(obj, dict):
            for conn_key in (
                "comments",
                "replies_connection",
                "display_comments_connection",
                "comment_rendering_instances",
            ):
                conn = obj.get(conn_key)
                if isinstance(conn, dict):
                    edges = conn.get("edges")
                    if edges:
                        _walk_edges(edges)
                    if conn_key == "comment_rendering_instances":
                        if isinstance(conn, dict):
                            for sub in conn.get("edges", []):
                                node = sub.get("node", {}) if isinstance(sub, dict) else {}
                                comment = node.get("comment") if isinstance(node, dict) else None
                                if comment:
                                    c = extract_comment_node(comment)
                                    if c:
                                        comments.append(c)
            for v in obj.values():
                _search(v)
        elif isinstance(obj, list):
            for item in obj:
                _search(item)

    for top in parsed_objects:
        _search(top)

    seen = set()
    unique = []
    for c in comments:
        cid = c["comment_id"]
        if cid and cid in seen:
            continue
        seen.add(cid)
        unique.append(c)

    return unique


class FeedbackMap:
    """Thread-safe mapping from feedback_id (base64) to post_id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}
        self._current_post_id: str = ""
        self._post_context: dict[str, dict] = {}

    def update_from_focused_story(self, meta: dict, parsed: list[dict]):
        """Extract post feedback_id from CometFocusedStoryViewUFIQuery."""
        variables = meta.get("variables", {})
        fb_id = variables.get("feedbackID", "")
        context = extract_post_context_from_focused_story(parsed)
        context["timestamp"] = normalize_post_timestamp(context.get("timestamp", ""))
        if fb_id:
            post_id, _ = decode_feedback_id(fb_id)
            if post_id:
                with self._lock:
                    self._current_post_id = post_id
                    self._map[fb_id] = post_id
                    rec = self._post_context.setdefault(post_id, {})
                    if context.get("timestamp"):
                        rec["timestamp"] = context["timestamp"]
                    if context.get("post_text") and not rec.get("post_text"):
                        rec["post_text"] = context["post_text"]
                return {"post_id": post_id, **context}
        return {}

    def resolve(self, feedback_b64: str) -> str:
        """Resolve a base64 feedback_id to a post_id."""
        with self._lock:
            if feedback_b64 in self._map:
                return self._map[feedback_b64]

        post_id, _ = decode_feedback_id(feedback_b64)
        if post_id:
            with self._lock:
                self._map[feedback_b64] = post_id
            return post_id
        with self._lock:
            return self._current_post_id

    @property
    def current_post_id(self) -> str:
        with self._lock:
            return self._current_post_id

    def get_post_context(self, post_id: str) -> dict:
        if not post_id:
            return {}
        with self._lock:
            return dict(self._post_context.get(post_id, {}))


class CommentStore:
    """Thread-safe store that merges comments by post_id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._posts: dict[str, dict[str, dict]] = {}
        self._parents: dict[str, dict[str, str]] = {}
        self._post_last_seen: dict[str, float] = {}
        self._post_order: deque[str] = deque()
        self._intercept_count = 0
        self._evicted_post_ids: set[str] = set()

    def add_comments(
        self, post_id: str, comments: list[dict], parent_comment_id: str = ""
    ) -> tuple[int, list[dict]]:
        if not post_id or not comments:
            return (0, [])
        added = 0
        with self._lock:
            if post_id not in self._posts:
                self._posts[post_id] = {}
                self._post_order.append(post_id)
            if post_id not in self._parents:
                self._parents[post_id] = {}
            bucket = self._posts[post_id]
            parent_map = self._parents[post_id]
            for c in comments:
                cid = c.get("comment_id", "")
                if not cid:
                    continue
                if cid not in bucket:
                    bucket[cid] = c
                    added += 1
                else:
                    existing = bucket[cid]
                    if c.get("body") and not existing.get("body"):
                        existing.update(c)
                    elif c.get("author") and not existing.get("author"):
                        existing.update(c)
                if parent_comment_id and cid != parent_comment_id:
                    parent_map[cid] = parent_comment_id
            self._post_last_seen[post_id] = time.time()
            self._intercept_count += 1
        return (added, [])

    @property
    def intercept_count(self) -> int:
        with self._lock:
            return self._intercept_count

    def stats(self) -> tuple[int, int]:
        """Return (num_posts, total_comments)."""
        with self._lock:
            total = sum(len(b) for b in self._posts.values())
            return len(self._posts), total

    def dump(self) -> dict[str, list[dict]]:
        with self._lock:
            return {
                pid: list(bucket.values())
                for pid, bucket in self._posts.items()
            }

    def dump_structural(self) -> list[dict]:
        """Return comment trees grouped by post_id."""
        with self._lock:
            records = []
            for post_id, bucket in self._posts.items():
                parent_map = self._parents.get(post_id, {})
                records.append(build_structural_record(post_id, bucket, parent_map))
            records.sort(key=lambda r: r["post_id"])
            return records

    def dump_structural_post(self, post_id: str) -> dict | None:
        """Return structural record for one post_id."""
        with self._lock:
            bucket = self._posts.get(post_id)
            if not bucket:
                return None
            parent_map = self._parents.get(post_id, {})
            return build_structural_record(post_id, bucket, parent_map)

    def has_post(self, post_id: str) -> bool:
        with self._lock:
            return post_id in self._posts

    def evict_oldest_posts(self, count: int = 1) -> list[dict]:
        """Evict oldest inserted posts (FIFO). Returns evicted structural records."""
        if count <= 0:
            return []
        with self._lock:
            evicted = []
            while self._post_order and len(evicted) < count:
                post_id = self._post_order.popleft()
                bucket = self._posts.pop(post_id, None)
                if not bucket:
                    continue
                parent_map = self._parents.pop(post_id, {})
                self._post_last_seen.pop(post_id, None)
                self._evicted_post_ids.add(post_id)
                evicted.append(build_structural_record(post_id, bucket, parent_map))
            return evicted

    def evict_old_posts(self, max_posts: int) -> list[dict]:
        """Evict oldest posts when exceeding max_posts. Returns evicted structural records."""
        if max_posts <= 0:
            return []
        with self._lock:
            current = len(self._posts)
            if current <= max_posts:
                return []

            to_remove_count = current - max_posts
            ordered = sorted(self._post_last_seen.items(), key=lambda x: x[1])
            to_remove = [pid for pid, _ in ordered[:to_remove_count]]
            evicted = []
            for post_id in to_remove:
                bucket = self._posts.pop(post_id, None)
                if not bucket:
                    continue
                parent_map = self._parents.pop(post_id, {})
                self._post_last_seen.pop(post_id, None)
                self._evicted_post_ids.add(post_id)
                evicted.append(build_structural_record(post_id, bucket, parent_map))
            return evicted


class CommentInterceptor:
    """CDP Network event listener that intercepts comment-related GraphQL responses."""

    def __init__(
        self,
        tab: pychrome.Tab,
        store: StoreProtocol,
        feedback_map: FeedbackMap,
        raw_path: Path,
        unknown_path: Path,
        structural_path: Path,
        max_posts_in_memory: int = 0,
    ):
        self.tab = tab
        self.store = store
        self.feedback_map = feedback_map
        self.raw_path = raw_path
        self.unknown_path = unknown_path
        self.structural_path = structural_path
        self.max_posts_in_memory = max_posts_in_memory
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self):
        self.tab.Network.enable()
        self.tab.Network.requestWillBeSent = self._on_request
        self.tab.Network.responseReceived = self._on_response
        self.tab.Network.loadingFinished = self._on_loading_finished

    def _cleanup_stale_pending(self):
        """Remove pending entries older than PENDING_TTL_SECONDS."""
        now = time.time()
        stale = [
            rid for rid, entry in self._pending.items()
            if now - entry.get("created_at", now) > PENDING_TTL_SECONDS
        ]
        for rid in stale:
            self._pending.pop(rid, None)

    def _on_request(self, **kwargs):
        request = kwargs.get("request", {})
        url = request.get("url", "")
        method = request.get("method", "")
        request_id = kwargs.get("requestId", "")

        if method != "POST" or GRAPHQL_URL not in url:
            return

        post_data = request.get("postData", "")
        meta = extract_request_meta(post_data)
        if not meta:
            return

        with self._lock:
            self._pending[request_id] = {
                "meta": meta,
                "ready": False,
                "created_at": time.time(),
            }

    def _on_response(self, **kwargs):
        """Mark the request as GraphQL-matched; actual body fetch waits for loadingFinished."""
        request_id = kwargs.get("requestId", "")
        response = kwargs.get("response", {})
        url = response.get("url", "")

        if GRAPHQL_URL not in url:
            return

        with self._lock:
            entry = self._pending.get(request_id)
            if entry:
                entry["ready"] = True

    def _on_loading_finished(self, **kwargs):
        """Body is fully buffered — now safe to call getResponseBody."""
        request_id = kwargs.get("requestId", "")

        with self._lock:
            self._cleanup_stale_pending()
            entry = self._pending.pop(request_id, None)
        if not entry or not entry.get("ready"):
            return

        meta = entry["meta"]
        query_name = identify_query(meta)

        try:
            body_result = self.tab.Network.getResponseBody(requestId=request_id)
            body = body_result.get("body", "")
        except Exception as e:
            print(f"\n  [!] getResponseBody failed: {e}", file=sys.stderr)
            return

        if not body:
            return

        parsed = parse_response_body(body)
        if not parsed:
            return

        if query_name == FOCUSED_STORY_QUERY:
            focused_context = self.feedback_map.update_from_focused_story(meta, parsed)
            self._sync_post_context_to_store(focused_context)
            return

        if query_name and query_name in COMMENTS_QUERIES:
            self._handle_comment_response(meta, parsed, query_name)
        else:
            self._save_unknown(meta, body)

    def _handle_comment_response(self, meta: dict, parsed: list[dict], query_name: str):
        variables = meta.get("variables", {})
        feedback_b64 = (
            variables.get("id")
            or variables.get("feedbackID")
            or variables.get("nodeID")
            or self._extract_feedback_from_response(parsed)
            or ""
        )
        post_id = self.feedback_map.resolve(feedback_b64) if feedback_b64 else ""
        if not post_id:
            post_id = self.feedback_map.current_post_id
        self._sync_post_context_to_store(
            {"post_id": post_id, **self.feedback_map.get_post_context(post_id)}
        )
        _, parent_comment_id = decode_feedback_id(feedback_b64) if feedback_b64 else ("", "")

        comments = extract_comments_from_response(parsed)

        if not comments and query_name == "CommentsListComponentsPaginationQuery":
            return

        raw_record = {
            "ts": time.time(),
            "query": query_name,
            "post_id": post_id,
            "feedback_id": feedback_b64,
            "comments_count": len(comments),
            "comments": comments,
        }
        append_jsonl(self.raw_path, raw_record)

        evicted: list[dict] = []
        if (
            self.max_posts_in_memory > 0
            and post_id
            and not self.store.has_post(post_id)
        ):
            n_posts, _ = self.store.stats()
            if n_posts >= self.max_posts_in_memory:
                evicted.extend(self.store.evict_oldest_posts(1))

        added, add_evicted = self.store.add_comments(
            post_id=post_id,
            comments=comments,
            parent_comment_id=parent_comment_id,
        )
        evicted.extend(add_evicted)

        for record in evicted:
            append_jsonl(
                self.structural_path,
                {
                    "ts": time.time(),
                    "event": "evicted",
                    "query": query_name,
                    **record,
                },
            )

        n_posts, n_total = self.store.stats()
        print(
            f"\n  [{query_name}] post={post_id} "
            f"new={added} batch={len(comments)} total={n_total} posts={n_posts}",
            flush=True,
        )

    def _sync_post_context_to_store(self, context: dict):
        post_id = context.get("post_id", "")
        if not post_id:
            return
        if not isinstance(self.store, MetadataStore):
            return
        self.store.update_post_metadata(
            post_id=post_id,
            post_text=context.get("post_text", ""),
            timestamp=context.get("timestamp", ""),
            source="graphql",
        )

    @staticmethod
    def _extract_feedback_from_response(parsed: list[dict]) -> str:
        for obj in parsed:
            node = deep_get(obj, "data", "node")
            if isinstance(node, dict) and node.get("__typename") == "Feedback":
                return node.get("id", "")
        return ""

    def flush_structural_buffer(self, query_name: str = "flush") -> int:
        """Flush all buffered structural posts to JSONL, skipping already-evicted posts."""
        evicted_ids = getattr(self.store, "_evicted_post_ids", set())
        records = [
            r for r in self.store.dump_structural()
            if r["post_id"] not in evicted_ids
        ]
        for record in records:
            append_jsonl(
                self.structural_path,
                {
                    "ts": time.time(),
                    "event": "flush",
                    "query": query_name,
                    **record,
                },
            )
        return len(records)

    def _save_unknown(self, meta: dict, body: str):
        try:
            try:
                parsed = json.loads(body)
                preview = parsed
            except json.JSONDecodeError:
                preview = body[:500]
            record = {
                "ts": time.time(),
                "friendly_name": meta.get("friendly_name", ""),
                "doc_id": meta.get("doc_id", ""),
                "body_preview": preview,
            }
            append_jsonl(self.unknown_path, record)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Passive FB comment collector via CDP Network interception"
    )
    parser.add_argument("--port", type=int, default=9222, help="Chrome debug port")
    parser.add_argument(
        "--raw", type=str, default="outputs/comments_raw.jsonl",
        help="Raw append-only log (JSONL)",
    )
    parser.add_argument(
        "--unknown", type=str, default="outputs/unused_graphql.jsonl",
        help="Unrecognized GraphQL responses (JSONL)",
    )
    parser.add_argument(
        "--structural",
        type=str,
        default="outputs/comments_structural.jsonl",
        help="Structured per-post output file (JSONL)",
    )
    parser.add_argument(
        "--max-posts-in-memory",
        type=int,
        default=10,
        help="Keep only latest N posts in memory (0 = unlimited)",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    raw_path = Path(args.raw)
    unknown_path = Path(args.unknown)
    structural_path = Path(args.structural)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    unknown_path.parent.mkdir(parents=True, exist_ok=True)
    structural_path.parent.mkdir(parents=True, exist_ok=True)

    tab = connect_to_chrome(args.port)
    store = CommentStore()
    feedback_map = FeedbackMap()

    interceptor = CommentInterceptor(
        tab=tab,
        store=store,
        feedback_map=feedback_map,
        raw_path=raw_path,
        unknown_path=unknown_path,
        structural_path=structural_path,
        max_posts_in_memory=args.max_posts_in_memory,
    )
    interceptor.start()

    def save_and_exit(sig=None, frame=None):
        flushed = interceptor.flush_structural_buffer()
        merged = store.dump()
        n_posts, n_comments = store.stats()
        output_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n\nSaved {n_comments} comments across {n_posts} posts to {output_path}")
        print(f"Flushed {flushed} buffered structural posts to {structural_path}")
        print(f"Structured log: {structural_path}")
        print(f"Raw log: {raw_path}")
        try:
            tab.stop()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, save_and_exit)
    signal.signal(signal.SIGTERM, save_and_exit)

    print(f"Connected to: {get_tab_url(tab)}")
    print(f"Output: {args.output} | Raw: {args.raw}")
    print()
    print("Listening for comment GraphQL responses...")
    print("Open a post and click 'View more comments' / 'View replies' in the browser.")
    print("Press Ctrl+C to stop and save.\n")

    while True:
        n_posts, n_comments = store.stats()
        intercepts = store.intercept_count
        status = (
            f"  [listening] intercepts: {intercepts} | "
            f"comments: {n_comments} | posts: {n_posts}"
        )
        print(f"\r{status}", end="", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
