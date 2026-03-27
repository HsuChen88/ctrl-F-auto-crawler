(() => {
  /**
   * Find all "expand" buttons on the current Facebook page.
   *
   * Targets:
   *   - "查看更多留言" / "查看更多回答" / "View more comments"
   *   - "查看 N 則回覆" / "查看全部的 N 則回覆" / "View N replies"
   *   - "查看之前的留言" / "View previous comments"
   *   - "更多回覆" / "顯示更多回覆" / "More replies" / "Show more replies"
   *   - "查看更多" / "See more" (comment body expand)
   *
   * Returns an array of { text, x, y, width, height, isInViewport, category }.
   * Coordinates are relative to the viewport (from getBoundingClientRect).
   *
   * This script is READ-ONLY: it never modifies the DOM or dispatches events.
   */

  const PATTERNS = [
    // "View N replies" / "View all N replies"
    {
      re: /^查看\s*(?:全部的?\s*)?\d+\s*則回覆$/,
      cat: "view_replies",
    },
    {
      re: /^View\s+(?:all\s+)?\d+\s+repl(?:y|ies)$/i,
      cat: "view_replies",
    },
    // "View more comments" / "View more answers"
    {
      re: /^查看更多(?:留言|回答|回覆)$/,
      cat: "view_more_comments",
    },
    {
      re: /^View\s+more\s+(?:comments|answers|replies)$/i,
      cat: "view_more_comments",
    },
    // "View previous comments"
    {
      re: /^查看之前的留言$/,
      cat: "view_previous",
    },
    {
      re: /^View\s+previous\s+comments$/i,
      cat: "view_previous",
    },
    // "More replies" / "Show more replies"
    {
      re: /^(?:顯示)?更多回覆$/,
      cat: "more_replies",
    },
    {
      re: /^(?:Show\s+)?[Mm]ore\s+replies$/i,
      cat: "more_replies",
    },
    // "See more" / "查看更多" (body expand within a comment)
    {
      re: /^(?:查看更多|顯示更多|See\s+more|Show\s+more|View\s+more)$/i,
      cat: "see_more",
    },
  ];

  function classify(text) {
    const t = (text || "").trim();
    if (!t) return null;
    for (const { re, cat } of PATTERNS) {
      if (re.test(t)) return { text: t, category: cat };
    }
    return null;
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function isInViewport(rect) {
    return (
      rect.top >= 0 &&
      rect.left >= 0 &&
      rect.bottom <= window.innerHeight &&
      rect.right <= window.innerWidth
    );
  }

  // Collect from role="button" elements
  const candidates = document.querySelectorAll(
    'div[role="button"], span[role="button"]'
  );

  const results = [];
  const seen = new Set();

  for (const el of candidates) {
    if (!isVisible(el)) continue;

    // Use textContent directly (these buttons are short text, no CSS reorder)
    const rawText = (el.textContent || "").trim().replace(/\s+/g, " ");
    const match = classify(rawText);
    if (!match) continue;

    const rect = el.getBoundingClientRect();
    // Deduplicate by position (within 2px tolerance)
    const key = `${Math.round(rect.x)},${Math.round(rect.y)}`;
    if (seen.has(key)) continue;
    seen.add(key);

    results.push({
      text: match.text,
      category: match.category,
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      isInViewport: isInViewport(rect),
    });
  }

  return results;
})();
