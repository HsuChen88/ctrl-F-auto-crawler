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
import base64
import json
import re
import signal
import sys
import threading
import time
from pathlib import Path

import pychrome

# GraphQL friendly names we care about
COMMENTS_QUERIES = {
    "CommentListComponentsRootQuery",
    "CommentsListComponentsPaginationQuery",
    "Depth1CommentsListPaginationQuery",
    "Depth2CommentsListPaginationQuery",
}

FOCUSED_STORY_QUERY = "CometFocusedStoryViewUFIQuery"

# Known doc_ids (fallback when friendly name is absent)
KNOWN_DOC_IDS = {
    "26150828281212332": "CommentListComponentsRootQuery",
    "26619250424347780": "CommentsListComponentsPaginationQuery",
    "26276906848640473": "Depth1CommentsListPaginationQuery",
    "26902344142700705": "CometFocusedStoryViewUFIQuery",
}

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

_FOR_LOOP_PREFIX = re.compile(r"^for\s*\(;;\)\s*;\s*")


def decode_feedback_id(b64: str) -> tuple[str, str]:
    """Decode a base64 feedback ID like 'ZmVlZGJhY2s6NzgzNzEy...'.

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


def extract_request_meta(post_data: str) -> dict:
    """Extract fb_api_req_friendly_name, doc_id, and variables from POST body."""
    if not post_data:
        return {}
    meta = {}
    for part in post_data.split("&"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        from urllib.parse import unquote_plus
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


def _deep_get(obj, *keys, default=None):
    """Safely traverse nested dicts."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return default
    return obj if obj is not None else default


def extract_comment_node(node: dict) -> dict | None:
    """Extract a single comment from a GraphQL edge node."""
    if not isinstance(node, dict):
        return None
    body_text = _deep_get(node, "body", "text", default="")
    author_name = _deep_get(node, "author", "name", default="")
    created_time = node.get("created_time")
    comment_id = node.get("legacy_fbid") or node.get("id", "")
    feedback_id = _deep_get(node, "feedback", "id", default="")

    if not body_text and not author_name:
        return None

    return {
        "comment_id": str(comment_id),
        "feedback_id": feedback_id,
        "author": author_name,
        "body": body_text,
        "created_time": created_time,
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
            # replies_connection.edges or display_comments_connection.edges
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
                    # Also look deeper inside rendering instances
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

    # Deduplicate by comment_id
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

    def update_from_focused_story(self, meta: dict, parsed: list[dict]):
        """Extract post feedback_id from CometFocusedStoryViewUFIQuery."""
        variables = meta.get("variables", {})
        fb_id = variables.get("feedbackID", "")
        if fb_id:
            post_id, _ = decode_feedback_id(fb_id)
            if post_id:
                with self._lock:
                    self._current_post_id = post_id
                    self._map[fb_id] = post_id

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


class CommentStore:
    """Thread-safe store that merges comments by post_id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._posts: dict[str, dict[str, dict]] = {}
        self._parents: dict[str, dict[str, str]] = {}
        self._post_last_seen: dict[str, float] = {}
        self._intercept_count = 0

    def add_comments(
        self, post_id: str, comments: list[dict], parent_comment_id: str = ""
    ) -> int:
        if not post_id or not comments:
            return 0
        added = 0
        with self._lock:
            if post_id not in self._posts:
                self._posts[post_id] = {}
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
        return added

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
                records.append(self._build_structural_record(post_id, bucket, parent_map))
            records.sort(key=lambda r: r["post_id"])
            return records

    def dump_structural_post(self, post_id: str) -> dict | None:
        """Return structural record for one post_id."""
        with self._lock:
            bucket = self._posts.get(post_id)
            if not bucket:
                return None
            parent_map = self._parents.get(post_id, {})
            return self._build_structural_record(post_id, bucket, parent_map)

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
                evicted.append(self._build_structural_record(post_id, bucket, parent_map))
            return evicted

    @staticmethod
    def _build_structural_record(
        post_id: str,
        bucket: dict[str, dict],
        parent_map: dict[str, str],
    ) -> dict:
        children_map: dict[str, list[str]] = {}
        root_ids = []

        for cid in bucket:
            parent_id = parent_map.get(cid, "")
            if parent_id and parent_id in bucket and parent_id != cid:
                children_map.setdefault(parent_id, []).append(cid)
            else:
                root_ids.append(cid)

        def build_node(comment_id: str, visited: set[str]) -> dict:
            if comment_id in visited:
                return {"comment_id": comment_id, "cycle_detected": True, "replies": []}
            visited.add(comment_id)
            node = dict(bucket[comment_id])
            node["parent_comment_id"] = parent_map.get(comment_id, "") or None
            child_ids = children_map.get(comment_id, [])
            child_ids_sorted = sorted(
                child_ids,
                key=lambda x: (bucket.get(x, {}).get("created_time") or 0, x),
            )
            node["replies"] = [build_node(cid, visited.copy()) for cid in child_ids_sorted]
            return node

        root_ids_sorted = sorted(
            root_ids,
            key=lambda x: (bucket.get(x, {}).get("created_time") or 0, x),
        )
        return {
            "post_id": post_id,
            "comments_count": len(bucket),
            "root_comments_count": len(root_ids_sorted),
            "comments": [build_node(cid, set()) for cid in root_ids_sorted],
        }


class CommentInterceptor:
    """CDP Network event listener that intercepts comment-related GraphQL responses."""

    def __init__(
        self,
        tab: pychrome.Tab,
        store: CommentStore,
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
            self._pending[request_id] = {"meta": meta, "ready": False}

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
            self.feedback_map.update_from_focused_story(meta, parsed)
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
        self._append_raw(raw_record)

        added = self.store.add_comments(
            post_id=post_id,
            comments=comments,
            parent_comment_id=parent_comment_id,
        )
        self._append_structural_snapshot(post_id, query_name)

        evicted = self.store.evict_old_posts(self.max_posts_in_memory)
        for record in evicted:
            self._append_structural_record(
                {
                    "ts": time.time(),
                    "event": "evicted",
                    "query": query_name,
                    **record,
                }
            )

        n_posts, n_total = self.store.stats()
        print(
            f"\n  [{query_name}] post={post_id} "
            f"new={added} batch={len(comments)} total={n_total} posts={n_posts}",
            flush=True,
        )

    @staticmethod
    def _extract_feedback_from_response(parsed: list[dict]) -> str:
        for obj in parsed:
            node = _deep_get(obj, "data", "node")
            if isinstance(node, dict) and node.get("__typename") == "Feedback":
                return node.get("id", "")
        return ""

    def _append_raw(self, record: dict):
        try:
            with open(self.raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"\n  [!] Failed to write raw log: {e}", file=sys.stderr)

    def _append_structural_snapshot(self, post_id: str, query_name: str):
        record = self.store.dump_structural_post(post_id)
        if not record:
            return
        self._append_structural_record(
            {
                "ts": time.time(),
                "event": "update",
                "query": query_name,
                **record,
            }
        )

    def _append_structural_record(self, record: dict):
        try:
            with open(self.structural_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"\n  [!] Failed to write structural log: {e}", file=sys.stderr)

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
            with open(self.unknown_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


def connect_to_chrome(port: int) -> pychrome.Tab:
    browser = pychrome.Browser(url=f"http://127.0.0.1:{port}")
    tabs = browser.list_tab()
    if not tabs:
        print("Error: No tabs found.", file=sys.stderr)
        sys.exit(1)

    fb_tab = None
    for tab in tabs:
        tab_info = getattr(tab, "_kwargs", {})
        url = tab_info.get("url", "") if isinstance(tab_info, dict) else ""
        if isinstance(url, str) and "facebook.com" in url:
            fb_tab = tab
            break

    if not fb_tab:
        print("Warning: No Facebook tab found. Using first tab.", file=sys.stderr)
        fb_tab = tabs[0]

    fb_tab.start()
    return fb_tab


def main():
    parser = argparse.ArgumentParser(
        description="Passive FB comment collector via CDP Network interception"
    )
    parser.add_argument("--port", type=int, default=9222, help="Chrome debug port")
    parser.add_argument(
        "--output", type=str, default="outputs/comments.json",
        help="Merged output file (JSON)",
    )
    parser.add_argument(
        "--raw", type=str, default="outputs/comments_raw.jsonl",
        help="Raw append-only log (JSONL)",
    )
    parser.add_argument(
        "--unknown", type=str, default="outputs/unknown_graphql.jsonl",
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
        default=0,
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
        merged = store.dump()
        n_posts, n_comments = store.stats()
        output_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n\nSaved {n_comments} comments across {n_posts} posts to {output_path}")
        print(f"Structured log: {structural_path}")
        print(f"Raw log: {raw_path}")
        try:
            tab.stop()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, save_and_exit)
    signal.signal(signal.SIGTERM, save_and_exit)

    tab_info = getattr(tab, "_kwargs", {})
    tab_url = tab_info.get("url", "") if isinstance(tab_info, dict) else ""
    print(f"Connected to: {tab_url}")
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
