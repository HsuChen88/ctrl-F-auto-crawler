#!/usr/bin/env python3
"""
Facebook Auto-Clicker — Expand comments/replies with human-like behavior.

Connects to an already-open Chrome (via CDP on --remote-debugging-port) and
automatically clicks "View more comments", "View N replies", "See more", etc.

Uses OS-level mouse input (pyautogui) so events are indistinguishable from
real user actions.  Coordinates are obtained by injecting a read-only JS
snippet via CDP Runtime.evaluate (same pattern as extract_posts.js).

Usage:
  1. ./start_chrome.bat                 # open Chrome with debug port
  2. Log into Facebook, navigate to a post/group.
  3. (Optional) In another terminal:    uv run python unified_collector.py
  4. uv run python auto_clicker.py [--dry-run]

The clicker clicks buttons; the existing interceptor captures GraphQL responses.
They communicate implicitly through the browser — no direct IPC needed.

Press Ctrl+C or move mouse to top-left corner to stop immediately.
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from pathlib import Path

import pychrome

from common import connect_to_chrome, get_tab_url, get_viewport_to_screen_offset, viewport_to_screen
from human_input import (
    HumanMouseSimulator,
    between_clicks_delay,
    human_delay,
    long_pause,
    very_long_pause,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIND_BUTTONS_JS = (Path(__file__).parent / "find_expand_buttons.js").read_text(
    encoding="utf-8"
)

# Categories in priority order (expand bodies first, then replies, then more)
CATEGORY_PRIORITY = [
    "see_more",           # expand truncated comment/post body
    "view_replies",       # "View N replies"
    "more_replies",       # "More replies" / "Show more replies"
    "view_more_comments", # "View more comments"
    "view_previous",      # "View previous comments"
]

# Captcha / checkpoint signals in the URL or DOM
CHECKPOINT_URL_PATTERNS = ["checkpoint", "captcha", "/login/"]
CHECKPOINT_DOM_SELECTORS = [
    "#captcha",
    "[data-testid='royal_login_form']",
]
CHECKPOINT_TEXT_PATTERNS = [
    "security check",
    "安全驗證",
    "確認你的身分",
    "Confirm your identity",
    "We need to verify",
    "請驗證你的帳號",
]

# Maximum consecutive "no buttons found" scans before stopping
MAX_EMPTY_SCANS = 5
# Maximum consecutive clicks that produce no new content before backoff
MAX_IDLE_CLICKS = 3


# ---------------------------------------------------------------------------
# Button finder (via CDP)
# ---------------------------------------------------------------------------


def find_buttons(tab: pychrome.Tab) -> list[dict]:
    """Inject find_expand_buttons.js and return the list of button descriptors."""
    try:
        result = tab.Runtime.evaluate(expression=FIND_BUTTONS_JS, returnByValue=True)
    except Exception as e:
        print(f"\n  [!] Runtime.evaluate error: {e}", file=sys.stderr)
        return []
    value = result.get("result", {}).get("value")
    if not isinstance(value, list):
        return []
    return value


# ---------------------------------------------------------------------------
# Safety monitor
# ---------------------------------------------------------------------------


def check_for_checkpoint(tab: pychrome.Tab) -> bool:
    """Return True if the page looks like a captcha or checkpoint."""
    # Check URL
    url = get_tab_url(tab)
    for pattern in CHECKPOINT_URL_PATTERNS:
        if pattern in url:
            return True

    # Check DOM for checkpoint elements and text
    js = """
    (() => {
        const selectors = %s;
        for (const sel of selectors) {
            if (document.querySelector(sel)) return true;
        }
        const text = document.body?.innerText || "";
        const patterns = %s;
        const lower = text.toLowerCase();
        for (const p of patterns) {
            if (lower.includes(p.toLowerCase())) return true;
        }
        return false;
    })()
    """ % (
        repr(CHECKPOINT_DOM_SELECTORS),
        repr(CHECKPOINT_TEXT_PATTERNS),
    )
    try:
        result = tab.Runtime.evaluate(expression=js, returnByValue=True)
        return bool(result.get("result", {}).get("value", False))
    except Exception:
        return False


def is_button_still_present(tab: pychrome.Tab, button: dict, tolerance: float = 30) -> bool:
    """Check if a button at roughly the same position still exists after clicking."""
    buttons = find_buttons(tab)
    for b in buttons:
        if b["category"] == button["category"]:
            dx = abs(b["x"] - button["x"])
            dy = abs(b["y"] - button["y"])
            if dx < tolerance and dy < tolerance:
                return True
    return False


# ---------------------------------------------------------------------------
# Scroll helpers
# ---------------------------------------------------------------------------


def scroll_into_view_if_needed(
    tab: pychrome.Tab,
    button: dict,
    mouse: HumanMouseSimulator,
    offset: tuple[int, int, float],
) -> dict | None:
    """If the button is outside the viewport, scroll it into view.

    Returns updated button dict with new coordinates, or None if lost.
    """
    if button["isInViewport"]:
        return button

    # Scroll in the direction of the button
    if button["y"] < 0:
        mouse.scroll_up(clicks=random.randint(3, 6))
    else:
        mouse.scroll_down(clicks=random.randint(3, 6))

    time.sleep(human_delay(median_sec=1.5, sigma=0.3))

    # Re-scan for the button
    buttons = find_buttons(tab)
    for b in buttons:
        if b["category"] == button["category"] and b["text"] == button["text"]:
            if b["isInViewport"]:
                return b
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace):
    tab = connect_to_chrome(args.port)
    mouse = HumanMouseSimulator()

    print(f"Connected to: {get_tab_url(tab)}")
    print(f"Mode: {'DRY RUN (no clicks)' if args.dry_run else 'LIVE'}")
    print(f"Max clicks: {args.max_clicks} | Max runtime: {args.max_runtime}m")
    print(f"Delay: {args.min_delay}-{args.max_delay}s between clicks")
    print()
    print("Press Ctrl+C or move mouse to top-left corner to stop.")
    print()

    start_time = time.time()
    click_count = 0
    empty_scan_streak = 0
    idle_click_streak = 0
    backoff_multiplier = 1.0

    while True:
        # Check runtime limit
        elapsed_min = (time.time() - start_time) / 60
        if elapsed_min >= args.max_runtime:
            print(f"\n  [i] Max runtime ({args.max_runtime}m) reached. Stopping.")
            break

        # Check click limit
        if click_count >= args.max_clicks:
            print(f"\n  [i] Max clicks ({args.max_clicks}) reached. Stopping.")
            break

        # Safety: check for captcha/checkpoint
        if check_for_checkpoint(tab):
            print("\n  [!!!] CHECKPOINT/CAPTCHA DETECTED. Stopping immediately.")
            print("  Please resolve manually in the browser, then restart.")
            break

        # Find buttons
        offset = get_viewport_to_screen_offset(tab)
        buttons = find_buttons(tab)

        if not buttons:
            empty_scan_streak += 1
            if empty_scan_streak >= MAX_EMPTY_SCANS:
                print(f"\n  [i] No buttons found for {MAX_EMPTY_SCANS} consecutive scans. Done.")
                break
            print(f"\r  [scan] No buttons found ({empty_scan_streak}/{MAX_EMPTY_SCANS}). Scrolling...", end="", flush=True)
            mouse.scroll_down(clicks=random.randint(3, 6))
            time.sleep(human_delay(median_sec=3.0, sigma=0.4))
            continue

        empty_scan_streak = 0

        # Sort by priority, then shuffle within same priority
        def sort_key(b):
            cat = b.get("category", "")
            try:
                return CATEGORY_PRIORITY.index(cat)
            except ValueError:
                return len(CATEGORY_PRIORITY)

        buttons.sort(key=sort_key)

        # Group by category and shuffle within each group
        grouped: dict[str, list[dict]] = {}
        for b in buttons:
            grouped.setdefault(b["category"], []).append(b)
        shuffled_buttons = []
        for cat in CATEGORY_PRIORITY:
            group = grouped.pop(cat, [])
            random.shuffle(group)
            shuffled_buttons.extend(group)
        # Any remaining categories
        for group in grouped.values():
            random.shuffle(group)
            shuffled_buttons.extend(group)

        # Randomly skip some buttons (10-20%)
        if len(shuffled_buttons) > 1:
            skip_count = max(0, int(len(shuffled_buttons) * random.uniform(0.1, 0.2)))
            if skip_count > 0:
                skip_indices = set(random.sample(range(len(shuffled_buttons)), skip_count))
                shuffled_buttons = [
                    b for i, b in enumerate(shuffled_buttons) if i not in skip_indices
                ]

        # Pick the first button
        button = shuffled_buttons[0]

        # Scroll into view if needed
        button = scroll_into_view_if_needed(tab, button, mouse, offset)
        if button is None:
            print("\r  [scan] Button lost after scrolling. Re-scanning...", end="", flush=True)
            time.sleep(human_delay(median_sec=2.0, sigma=0.3))
            continue

        # Recompute offset (scroll may have changed window state)
        offset = get_viewport_to_screen_offset(tab)

        # Convert to screen coordinates
        center_x = button["x"] + button["width"] / 2
        center_y = button["y"] + button["height"] / 2
        screen_x, screen_y = viewport_to_screen(center_x, center_y, offset)

        if args.dry_run:
            print(
                f"  [dry-run #{click_count + 1}] "
                f"Would click: \"{button['text']}\" ({button['category']}) "
                f"at screen ({screen_x}, {screen_y})"
            )
            click_count += 1
            time.sleep(1)
            continue

        # Occasionally wander the mouse before clicking (look human)
        if random.random() < 0.15:
            mouse.wander()
            time.sleep(human_delay(median_sec=1.5, sigma=0.4))

        # Click!
        mouse.click(screen_x, screen_y)
        click_count += 1

        cat_display = button["category"]
        print(
            f"\n  [click #{click_count}] \"{button['text']}\" ({cat_display}) "
            f"at ({screen_x}, {screen_y}) | elapsed={elapsed_min:.1f}m"
        )

        # Wait for content to load
        time.sleep(human_delay(median_sec=2.0, sigma=0.3))

        # Check if the button we just clicked is still there (= click didn't work)
        if is_button_still_present(tab, button):
            idle_click_streak += 1
            if idle_click_streak >= MAX_IDLE_CLICKS:
                backoff_multiplier = min(backoff_multiplier * 1.5, 5.0)
                print(f"  [backoff] {idle_click_streak} idle clicks. Multiplier: {backoff_multiplier:.1f}x")
                if idle_click_streak >= MAX_IDLE_CLICKS * 2:
                    pause = long_pause()
                    print(f"  [long pause] Waiting {pause:.0f}s ...")
                    time.sleep(pause)
                    idle_click_streak = 0
        else:
            idle_click_streak = 0
            backoff_multiplier = max(1.0, backoff_multiplier * 0.8)

        # Inter-click delay
        delay = between_clicks_delay(args.min_delay, args.max_delay) * backoff_multiplier
        # Occasionally take a longer break
        if click_count > 0 and click_count % random.randint(5, 10) == 0:
            delay = long_pause()
            print(f"  [break] Taking a break: {delay:.0f}s ...")
        # Rare very long pause
        if random.random() < 0.02:
            delay = very_long_pause()
            print(f"  [long break] Extended pause: {delay:.0f}s ...")

        time.sleep(delay)

    # Summary
    elapsed = (time.time() - start_time) / 60
    print(f"\n{'='*50}")
    print(f"Done. Clicks: {click_count} | Runtime: {elapsed:.1f} minutes")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Auto-click Facebook expand buttons with human-like behavior"
    )
    parser.add_argument("--port", type=int, default=9222, help="Chrome debug port")
    parser.add_argument(
        "--max-clicks",
        type=int,
        default=50,
        help="Max button clicks per session (default: 50)",
    )
    parser.add_argument(
        "--max-runtime",
        type=float,
        default=120,
        help="Max runtime in minutes (default: 120)",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=8,
        help="Min delay between clicks in seconds (default: 8)",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=20,
        help="Max delay between clicks in seconds (default: 20)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find buttons and log but do not click",
    )
    args = parser.parse_args()

    def on_signal(sig, frame):
        print("\n\n  [!] Interrupted. Stopping.")
        sys.exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    run(args)


if __name__ == "__main__":
    main()
