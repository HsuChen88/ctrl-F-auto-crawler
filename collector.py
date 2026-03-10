#!/usr/bin/env python3
"""
Facebook Group Post Collector — Passive Monitor Mode.

The script ONLY reads the DOM. All scrolling, clicking, and navigation
is done by YOU in the browser. The script never touches the page.

Usage:
  1. ./start_chrome.sh
  2. Log into Facebook, navigate to your group page.
  3. uv run python collector.py
  4. Scroll the page yourself. The script silently collects what you see.
  5. Press Ctrl+C when done. Results are saved to JSON.
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path

import pychrome

EXTRACT_POSTS_JS = (Path(__file__).parent / "extract_posts.js").read_text()


class PostCollector:
    def __init__(self):
        self.posts: dict[str, dict] = {}

    def merge(self, new_posts: list[dict]) -> int:
        added = 0
        for post in new_posts:
            key = post.get("post_text", "")[:120]
            if not key:
                continue
            if key not in self.posts:
                self.posts[key] = post
                added += 1
            else:
                existing = self.posts[key]
                if len(post.get("comments", [])) > len(existing.get("comments", [])):
                    self.posts[key] = post
        return added

    def results(self) -> list[dict]:
        return list(self.posts.values())


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


def get_tab_url(tab: pychrome.Tab) -> str:
    tab_info = getattr(tab, "_kwargs", {})
    if not isinstance(tab_info, dict):
        return ""
    url = tab_info.get("url", "")
    return url if isinstance(url, str) else ""


def extract_posts(tab: pychrome.Tab) -> list[dict]:
    try:
        result = tab.Runtime.evaluate(expression=EXTRACT_POSTS_JS, returnByValue=True)
    except Exception as e:
        print(f"  [!] CDP error: {e}", file=sys.stderr)
        return []
    if "exceptionDetails" in result:
        return []
    return result.get("result", {}).get("value", [])


def main():
    parser = argparse.ArgumentParser(description="Passive FB group post collector")
    parser.add_argument("--port", type=int, default=9222, help="Chrome debug port")
    parser.add_argument("--output", type=str, default="outputs/posts.json", help="Output file")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="Seconds between each DOM snapshot (default: 3)")
    args = parser.parse_args()

    tab = connect_to_chrome(args.port)
    collector = PostCollector()
    output_path = Path(args.output)

    def save_and_exit(sig=None, frame=None):
        results = collector.results()
        output_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n\nSaved {len(results)} posts to {output_path}")
        try:
            tab.stop()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, save_and_exit)
    signal.signal(signal.SIGTERM, save_and_exit)

    print(f"Connected to: {get_tab_url(tab)}")
    print(f"Polling DOM every {args.interval}s. Output: {args.output}")
    print()
    print("Now scroll the Facebook page yourself.")
    print("The script will silently capture whatever posts are visible.")
    print("Press Ctrl+C to stop and save.\n")

    snapshot_count = 0
    while True:
        posts = extract_posts(tab)
        added = collector.merge(posts)
        snapshot_count += 1
        total = len(collector.results())

        status = f"  [#{snapshot_count}] visible: {len(posts)} | new: {added} | total: {total}"
        print(f"\r{status}", end="", flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
