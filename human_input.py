"""Human-like mouse movement and timing simulation.

Uses OS-level input (pyautogui) so events are indistinguishable from real
user input in the browser.  Mouse paths follow Bezier curves with jitter;
delays use log-normal distributions to mimic human reaction times.
"""

from __future__ import annotations

import math
import random
import time

import pyautogui

# Disable pyautogui's built-in pause and failsafe (we handle timing ourselves)
pyautogui.PAUSE = 0
pyautogui.FAILSAFE = True  # keep failsafe: move to corner to abort


# ---------------------------------------------------------------------------
# Bezier curve helpers
# ---------------------------------------------------------------------------


def _bernstein(n: int, k: int, t: float) -> float:
    """Bernstein basis polynomial."""
    return math.comb(n, k) * (t ** k) * ((1 - t) ** (n - k))


def _bezier_point(
    control_points: list[tuple[float, float]], t: float
) -> tuple[float, float]:
    """Evaluate a Bezier curve at parameter t (0..1)."""
    n = len(control_points) - 1
    x = sum(_bernstein(n, k, t) * p[0] for k, p in enumerate(control_points))
    y = sum(_bernstein(n, k, t) * p[1] for k, p in enumerate(control_points))
    return (x, y)


def _generate_control_points(
    start: tuple[float, float],
    end: tuple[float, float],
    num_control: int = 3,
) -> list[tuple[float, float]]:
    """Generate random control points for a natural-looking Bezier curve."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)
    spread = max(dist * 0.3, 30)  # how far control points deviate

    points = [start]
    for i in range(1, num_control + 1):
        frac = i / (num_control + 1)
        mid_x = start[0] + dx * frac + random.gauss(0, spread * 0.5)
        mid_y = start[1] + dy * frac + random.gauss(0, spread * 0.3)
        points.append((mid_x, mid_y))
    points.append(end)
    return points


def _ease_in_out(t: float) -> float:
    """Smooth ease-in-ease-out (slow at start/end, fast in middle)."""
    return t * t * (3 - 2 * t)


# ---------------------------------------------------------------------------
# Mouse simulator
# ---------------------------------------------------------------------------


class HumanMouseSimulator:
    """Simulates human-like mouse movement and clicks via pyautogui."""

    def __init__(
        self,
        jitter_px: float = 2.0,
        min_steps: int = 20,
        max_steps: int = 80,
    ):
        self.jitter_px = jitter_px
        self.min_steps = min_steps
        self.max_steps = max_steps

    def move_to(
        self,
        x: int,
        y: int,
        duration: float | None = None,
        click_offset_px: int = 10,
    ):
        """Move the OS cursor to (x, y) with small random offset, via Bezier curve.

        Args:
            x, y: Target screen coordinates (center of element).
            duration: Total movement time in seconds.  Auto-calculated if None.
            click_offset_px: Random offset from exact center (pixels).
        """
        # Add random offset so we don't always hit dead center
        target_x = x + random.randint(-click_offset_px, click_offset_px)
        target_y = y + random.randint(-click_offset_px, click_offset_px)

        current = pyautogui.position()
        start = (float(current[0]), float(current[1]))
        end = (float(target_x), float(target_y))

        dist = math.hypot(end[0] - start[0], end[1] - start[1])

        if duration is None:
            # 0.3s for short moves, up to 1.5s for long ones
            duration = 0.3 + min(dist / 800, 1.2)
            duration *= random.uniform(0.85, 1.15)

        num_control = random.choice([2, 3, 3, 4])
        control_points = _generate_control_points(start, end, num_control)

        # Number of steps scales with distance
        steps = max(
            self.min_steps,
            min(self.max_steps, int(dist / 5)),
        )

        step_delay = duration / steps
        for i in range(steps + 1):
            t = _ease_in_out(i / steps)
            px, py = _bezier_point(control_points, t)

            # Add micro-jitter (simulates hand tremor)
            if 0 < i < steps:  # no jitter on start/end
                px += random.gauss(0, self.jitter_px)
                py += random.gauss(0, self.jitter_px)

            pyautogui.moveTo(int(px), int(py), _pause=False)
            time.sleep(step_delay)

    def click(self, x: int, y: int, click_offset_px: int = 8):
        """Move to (x, y) and perform a human-like click."""
        self.move_to(x, y, click_offset_px=click_offset_px)

        # Small pre-click pause (finger hovering before pressing)
        time.sleep(random.uniform(0.05, 0.2))

        # Randomize hold duration (time between mousedown and mouseup)
        hold_time = random.uniform(0.04, 0.12)
        pyautogui.mouseDown()
        time.sleep(hold_time)
        pyautogui.mouseUp()

    def scroll_down(self, clicks: int | None = None):
        """Scroll down with human-like variable speed.

        Args:
            clicks: Number of scroll wheel ticks.  Random 2-5 if None.
        """
        if clicks is None:
            clicks = random.randint(2, 5)

        # Scroll in small increments with slight delays
        for _ in range(clicks):
            pyautogui.scroll(-1)  # negative = scroll down
            time.sleep(random.uniform(0.02, 0.08))

    def scroll_up(self, clicks: int | None = None):
        """Scroll up (for overshoot correction)."""
        if clicks is None:
            clicks = random.randint(1, 2)
        for _ in range(clicks):
            pyautogui.scroll(1)
            time.sleep(random.uniform(0.02, 0.08))

    def wander(self, bounds: tuple[int, int, int, int] | None = None):
        """Move the mouse to a random position (simulates idle movement).

        Args:
            bounds: (x_min, y_min, x_max, y_max) area to wander within.
                    Uses screen size if None.
        """
        if bounds is None:
            sw, sh = pyautogui.size()
            bounds = (100, 100, sw - 100, sh - 100)

        target_x = random.randint(bounds[0], bounds[2])
        target_y = random.randint(bounds[1], bounds[3])
        self.move_to(target_x, target_y, click_offset_px=0)


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------


def human_delay(median_sec: float = 3.0, sigma: float = 0.5) -> float:
    """Return a log-normally distributed delay (seconds).

    Log-normal models human reaction times well: usually fast,
    occasionally slow, never negative.
    """
    return random.lognormvariate(math.log(median_sec), sigma)


def reading_pause(num_comments: int = 1) -> float:
    """Pause proportional to the amount of new content visible."""
    base = human_delay(median_sec=3.0, sigma=0.4)
    # ~1-2 extra seconds per comment
    per_comment = num_comments * random.uniform(0.8, 2.0)
    return base + per_comment


def between_clicks_delay(min_sec: float = 8.0, max_sec: float = 20.0) -> float:
    """Delay between consecutive button clicks."""
    median = (min_sec + max_sec) / 2
    delay = human_delay(median_sec=median, sigma=0.4)
    return max(min_sec, min(delay, max_sec * 2))


def long_pause() -> float:
    """Occasional long pause (simulates user doing something else)."""
    return human_delay(median_sec=60.0, sigma=0.6)


def very_long_pause() -> float:
    """Rare very long pause (simulates user leaving the computer)."""
    return human_delay(median_sec=300.0, sigma=0.5)
