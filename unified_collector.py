#!/usr/bin/env python3
"""
Unified Facebook collector:
- DOM snapshots (posts + visible comments) from collector.py
- GraphQL comment interception from comment_collector.py

Builds a per-post in-memory frame, merges incremental reply/comment data, and
writes structural JSONL records when evicting old posts or on final flush.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from collector import extract_posts
from comment_collector import (
    CommentInterceptor,
    FeedbackMap,
    normalize_post_timestamp,
)
from common import (
    append_jsonl,
    build_structural_record,
    connect_to_chrome,
    feedback_id,
    get_tab_url,
)


class UnifiedPostStore:
    """Thread-safe in-memory post/comment store for DOM + GraphQL merges."""

    def __init__(self, max_posts_in_memory: int = 10):
        self._lock = threading.Lock()
        self._posts: dict[str, dict] = {}
        self._post_order: deque[str] = deque()
        self._intercept_count = 0
        self._max_posts_in_memory = max_posts_in_memory
        self._evicted_post_ids: set[str] = set()

    @staticmethod
    def _new_post_frame(post: dict) -> dict:
        return {
            "post_id": post.get("post_id", ""),
            "post_text": post.get("post_text", ""),
            "post_link": post.get("post_link", ""),
            "timestamp": "",
            "timestamp_source": "",
            "comment_count": post.get("comment_count") or 0,
            "comments": {},
            "parent_map": {},
            "last_seen": time.time(),
        }

    @staticmethod
    def _normalize_time_with_cache(
        value: str,
        cache: dict[str, str],
        now_dt: datetime,
    ) -> str:
        raw = (value or "").strip()
        if not raw:
            return ""
        cached = cache.get(raw)
        if cached is not None:
            return cached
        normalized = normalize_post_timestamp(raw, now=now_dt)
        cache[raw] = normalized
        return normalized

    @staticmethod
    def _is_relative_timestamp(value: str) -> bool:
        t = (value or "").strip().lower()
        if not t:
            return False
        relative_tokens = (
            "剛剛",
            "秒",
            "分鐘",
            "小時",
            "天前",
            "週",
            "小时前",
            "分钟前",
            "seconds ago",
            "minutes ago",
            "hours ago",
            "days ago",
            "weeks ago",
            "ago",
            "sec",
            "min",
            "hr",
            "wk",
        )
        return any(token in t for token in relative_tokens)

    @classmethod
    def _pick_better_timestamp(cls, existing: str, incoming: str) -> str:
        current = (existing or "").strip()
        candidate = (incoming or "").strip()
        if not current:
            return candidate
        if not candidate:
            return current
        current_relative = cls._is_relative_timestamp(current)
        candidate_relative = cls._is_relative_timestamp(candidate)
        if current_relative and not candidate_relative:
            return candidate
        if not current_relative and candidate_relative:
            return current
        return current if len(current) >= len(candidate) else candidate

    def _set_timestamp_locked(self, frame: dict, timestamp: str, source: str):
        candidate = normalize_post_timestamp(timestamp or "")
        if not candidate:
            return
        current = frame.get("timestamp") or ""
        current_source = frame.get("timestamp_source") or ""

        if source == "dom":
            frame["timestamp"] = candidate
            frame["timestamp_source"] = "dom"
            return

        if current_source == "dom":
            return
        if not current:
            frame["timestamp"] = candidate
            frame["timestamp_source"] = "graphql"
            return
        frame["timestamp"] = self._pick_better_timestamp(current, candidate)
        frame["timestamp_source"] = "graphql"

    @classmethod
    def _normalize_dom_comment(
        cls,
        post_id: str,
        comment: dict,
        time_cache: dict[str, str],
        now_dt: datetime,
    ) -> dict | None:
        cid = str(comment.get("comment_id") or "")
        body = comment.get("text") or ""
        author = comment.get("author") or ""
        if not cid and not body and not author:
            return None
        return {
            "comment_id": cid,
            "feedback_id": comment.get("feedback_id") or feedback_id(post_id, cid),
            "author": author,
            "body": body,
            "time": cls._normalize_time_with_cache(comment.get("time") or "", time_cache, now_dt),
            "created_time": None,
            "parent_comment_id": comment.get("parent_comment_id"),
            "source": "dom",
        }

    @classmethod
    def _normalize_graphql_comment(
        cls,
        comment: dict,
        time_cache: dict[str, str],
        now_dt: datetime,
        parent_comment_id: str = "",
    ) -> dict | None:
        cid = str(comment.get("comment_id") or "")
        body = comment.get("body") or ""
        author = comment.get("author") or ""
        if not cid and not body and not author:
            return None
        parent_id = parent_comment_id or comment.get("parent_comment_id") or None
        return {
            "comment_id": cid,
            "feedback_id": comment.get("feedback_id") or "",
            "author": author,
            "body": body,
            "time": cls._normalize_time_with_cache(comment.get("time") or "", time_cache, now_dt),
            "created_time": comment.get("created_time"),
            "parent_comment_id": parent_id,
            "source": "graphql",
        }

    @staticmethod
    def _merge_comment(existing: dict, incoming: dict):
        for field in ("feedback_id", "author", "body", "time", "created_time"):
            new_val = incoming.get(field)
            old_val = existing.get(field)
            if new_val and not old_val:
                existing[field] = new_val
            elif field == "created_time" and isinstance(new_val, int):
                if not isinstance(old_val, int) or new_val < old_val:
                    existing[field] = new_val

        incoming_parent = incoming.get("parent_comment_id")
        if incoming_parent:
            existing["parent_comment_id"] = incoming_parent
        if incoming.get("source") == "graphql":
            existing["source"] = "graphql"

    def _evict_if_needed_locked(self) -> list[dict]:
        if self._max_posts_in_memory <= 0:
            return []
        evicted = []
        while len(self._posts) > self._max_posts_in_memory and self._post_order:
            pid = self._post_order.popleft()
            post = self._posts.pop(pid, None)
            if not post:
                continue
            self._evicted_post_ids.add(pid)
            evicted.append(self._build_structural_record(post))
        return evicted

    def has_post(self, post_id: str) -> bool:
        with self._lock:
            return post_id in self._posts

    def stats(self) -> tuple[int, int]:
        with self._lock:
            total = sum(len(p["comments"]) for p in self._posts.values())
            return len(self._posts), total

    @property
    def intercept_count(self) -> int:
        with self._lock:
            return self._intercept_count

    def evict_oldest_posts(self, count: int = 1) -> list[dict]:
        if count <= 0:
            return []
        with self._lock:
            evicted = []
            while self._post_order and len(evicted) < count:
                pid = self._post_order.popleft()
                post = self._posts.pop(pid, None)
                if not post:
                    continue
                self._evicted_post_ids.add(pid)
                evicted.append(self._build_structural_record(post))
            return evicted

    def merge_dom_posts(self, posts: list[dict]) -> tuple[int, list[dict]]:
        """Merge DOM snapshots. Returns (new_posts_added, evicted_structural_records)."""
        added_posts = 0
        now_dt = datetime.now()
        time_cache: dict[str, str] = {}
        with self._lock:
            for post in posts:
                post_id = post.get("post_id") or ""
                if not post_id:
                    continue
                if post_id not in self._posts:
                    self._posts[post_id] = self._new_post_frame(post)
                    self._post_order.append(post_id)
                    added_posts += 1
                frame = self._posts[post_id]
                frame["post_text"] = post.get("post_text") or frame.get("post_text") or ""
                frame["post_link"] = post.get("post_link") or frame.get("post_link") or ""
                self._set_timestamp_locked(
                    frame,
                    self._normalize_time_with_cache(post.get("timestamp") or "", time_cache, now_dt),
                    source="dom",
                )
                frame["comment_count"] = max(
                    int(frame.get("comment_count") or 0),
                    int(post.get("comment_count") or 0),
                )
                frame["last_seen"] = time.time()

                for c in post.get("comments", []):
                    normalized = self._normalize_dom_comment(post_id, c, time_cache, now_dt)
                    if not normalized:
                        continue
                    cid = normalized.get("comment_id") or ""
                    if not cid:
                        continue
                    bucket = frame["comments"]
                    if cid not in bucket:
                        bucket[cid] = normalized
                    else:
                        self._merge_comment(bucket[cid], normalized)

                    parent_id = normalized.get("parent_comment_id")
                    if parent_id and parent_id != cid:
                        frame["parent_map"][cid] = parent_id

            evicted = self._evict_if_needed_locked()
            return added_posts, evicted

    def add_comments(
        self, post_id: str, comments: list[dict], parent_comment_id: str = ""
    ) -> tuple[int, list[dict]]:
        """Merge GraphQL comment batch. Returns (added_comments, evicted_structural_records)."""
        if not post_id or not comments:
            return (0, [])
        added = 0
        now_dt = datetime.now()
        time_cache: dict[str, str] = {}
        with self._lock:
            if post_id not in self._posts:
                self._posts[post_id] = self._new_post_frame({"post_id": post_id})
                self._post_order.append(post_id)
            frame = self._posts[post_id]
            bucket = frame["comments"]
            parent_map = frame["parent_map"]

            for c in comments:
                normalized = self._normalize_graphql_comment(
                    c,
                    time_cache=time_cache,
                    now_dt=now_dt,
                    parent_comment_id=parent_comment_id,
                )
                if not normalized:
                    continue
                cid = normalized.get("comment_id") or ""
                if not cid:
                    continue
                if cid not in bucket:
                    bucket[cid] = normalized
                    added += 1
                else:
                    self._merge_comment(bucket[cid], normalized)
                parent_id = normalized.get("parent_comment_id")
                if parent_id and parent_id != cid:
                    parent_map[cid] = parent_id
                    bucket[cid]["parent_comment_id"] = parent_id

            frame["last_seen"] = time.time()
            self._intercept_count += 1
            evicted = self._evict_if_needed_locked()
            return (added, evicted)

    def update_post_metadata(
        self,
        post_id: str,
        post_text: str = "",
        timestamp: str = "",
        source: str = "graphql",
    ):
        if not post_id:
            return
        with self._lock:
            if post_id not in self._posts:
                self._posts[post_id] = self._new_post_frame({"post_id": post_id})
                self._post_order.append(post_id)
            frame = self._posts[post_id]
            if source == "dom":
                frame["post_text"] = post_text or frame.get("post_text") or ""
            elif post_text and not frame.get("post_text"):
                frame["post_text"] = post_text
            self._set_timestamp_locked(frame, timestamp, source=source)
            frame["last_seen"] = time.time()

    def dump(self) -> dict[str, list[dict]]:
        with self._lock:
            return {
                pid: list(frame["comments"].values())
                for pid, frame in self._posts.items()
            }

    def dump_structural(self) -> list[dict]:
        with self._lock:
            out = [self._build_structural_record(p) for p in self._posts.values()]
            out.sort(key=lambda r: r["post_id"])
            return out

    def _build_structural_record(self, frame: dict) -> dict:
        post_id = frame.get("post_id") or ""
        bucket: dict[str, dict] = frame.get("comments", {})
        parent_map: dict[str, str] = frame.get("parent_map", {})
        return build_structural_record(
            post_id,
            bucket,
            parent_map,
            extra_fields={
                "post_text": frame.get("post_text") or "",
                "timestamp": frame.get("timestamp") or "",
                "comment_count": int(frame.get("comment_count") or 0),
            },
        )


def main():
    parser = argparse.ArgumentParser(description="Unified FB DOM + GraphQL collector")
    parser.add_argument("--port", type=int, default=9222, help="Chrome debug port")
    parser.add_argument(
        "--raw",
        type=str,
        default="outputs/intercept_graphsql.jsonl",
        help="Raw GraphQL capture output (JSONL)",
    )
    parser.add_argument(
        "--unknown",
        type=str,
        default="outputs/unknown_graphql.jsonl",
        help="Unknown GraphQL output (JSONL)",
    )
    parser.add_argument(
        "--structural",
        type=str,
        default="outputs/comments_structural.jsonl",
        help="Structured per-post output (JSONL)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Seconds between each DOM snapshot",
    )
    parser.add_argument(
        "--max-posts-in-memory",
        type=int,
        default=10,
        help="Keep at most N posts in memory (0 = unlimited)",
    )
    args = parser.parse_args()

    raw_path = Path(args.raw)
    unknown_path = Path(args.unknown)
    structural_path = Path(args.structural)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    unknown_path.parent.mkdir(parents=True, exist_ok=True)
    unknown_path.parent.mkdir(parents=True, exist_ok=True)
    structural_path.parent.mkdir(parents=True, exist_ok=True)

    tab = connect_to_chrome(args.port)
    store = UnifiedPostStore(max_posts_in_memory=args.max_posts_in_memory)
    feedback_map = FeedbackMap()

    interceptor = CommentInterceptor(
        tab=tab,
        store=store,
        feedback_map=feedback_map,
        raw_path=raw_path,
        unknown_path=unknown_path,
        structural_path=structural_path,
        max_posts_in_memory=0,
    )
    interceptor.start()

    def write_evicted(evicted: list[dict], event: str, query: str):
        for record in evicted:
            append_jsonl(
                structural_path,
                {
                    "ts": time.time(),
                    "event": event,
                    "query": query,
                    **record,
                },
            )

    def save_and_exit(sig=None, frame=None):
        records = [
            r for r in store.dump_structural()
            if r["post_id"] not in store._evicted_post_ids
        ]
        for record in records:
            append_jsonl(
                structural_path,
                {
                    "ts": time.time(),
                    "event": "flush",
                    "query": "flush",
                    **record,
                },
            )
        n_posts, n_comments = store.stats()
        print(f"\n\nFlushed {len(records)} buffered structural posts to {structural_path}")
        print(f"Total: {n_comments} comments across {n_posts} posts")
        print(f"Structural: {structural_path} | Raw: {raw_path}")
        try:
            tab.stop()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, save_and_exit)
    signal.signal(signal.SIGTERM, save_and_exit)

    print(f"Connected to: {get_tab_url(tab)}")
    print(
        f"DOM interval: {args.interval}s | max posts in memory: {args.max_posts_in_memory}"
    )
    print(f"Structural: {args.structural} | Raw: {args.raw}")
    print()
    print("Now scroll and click in Facebook manually.")
    print("Collector will merge DOM snapshots and GraphQL comment responses.")
    print("Press Ctrl+C to stop and save.\n")

    snapshot_count = 0
    while True:
        posts = extract_posts(tab)
        added_posts, evicted = store.merge_dom_posts(posts)
        if evicted:
            write_evicted(evicted, event="evicted", query="dom_snapshot")

        snapshot_count += 1
        n_posts, n_comments = store.stats()
        intercepts = store.intercept_count
        status = (
            f"  [#{snapshot_count}] visible_posts={len(posts)} new_posts={added_posts} "
            f"| intercepts={intercepts} | comments={n_comments} | posts={n_posts}"
        )
        print(f"\r{status}", end="", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
