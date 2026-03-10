(() => {
  /**
   * Extract all visible FB group posts + their comments from the current DOM.
   *
   * Strategy:
   *  - Each post lives in a div[aria-posinset] (FB's virtualized list item).
   *  - Post body text is inside div[data-ad-rendering-role="story_message"].
   *  - Timestamp can be parsed from comment aria-labels or the post's <a> links.
   *  - Comments are div[role="article"] nested inside the post container.
   *
   * Locale: supports 中文 (則留言、月/日/週、的留言、查看更多/顯示更多) and English
   * (comments, month names, AM/PM, "Comment by …", See more/Show more/View more).
   *
   * FB anti-scrape: timestamps use CSS reordering (position:absolute; top:3em)
   * to shuffle visible characters. We handle this by reading aria-label on
   * comment articles and the post's timestamp link.
   */

  function getVisibleText(el) {
    if (!el) return "";
    const clone = el.cloneNode(true);
    clone.querySelectorAll('[aria-hidden="true"]').forEach((h) => h.remove());
    clone
      .querySelectorAll('[style*="position: absolute"]')
      .forEach((h) => h.remove());
    return (clone.textContent || "").trim().replace(/\s+/g, " ");
  }

  function extractTimestampFromLink(postEl) {
    const links = postEl.querySelectorAll("a[href]");
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      if (href.includes("/posts/") || href.includes("permalink")) {
        const visibleText = getVisibleText(link);
        if (visibleText && /\d/.test(visibleText)) {
          const zh =
            visibleText.includes("月") ||
            visibleText.includes("日") ||
            visibleText.includes("小時") ||
            visibleText.includes("分鐘") ||
            visibleText.includes("週") ||
            /\d+[hmd]/.test(visibleText);
          const en =
            /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b/i.test(visibleText) ||
            /\d+\s*(?:hr|min|hour|minute|day|week|wk|h|m|d|w)s?\s*(?:ago)?/i.test(visibleText) ||
            /(?:AM|PM|\d{1,2}:\d{2})/i.test(visibleText) ||
            /at\s+\d/i.test(visibleText);
          if (zh || en) return visibleText;
        }
      }
    }

    const spans = postEl.querySelectorAll("span");
    for (const span of spans) {
      const ariaLabel = span.getAttribute("aria-labelledby");
      if (ariaLabel) {
        const labelEl = document.getElementById(ariaLabel);
        if (labelEl) return labelEl.textContent.trim();
      }
    }

    return "";
  }

  function extractPostLink(postEl) {
    const links = postEl.querySelectorAll('a[href*="/posts/"]');
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      const path = href.split("?")[0];
      const match = path.match(/\/posts\/(\d+)/);
      if (match) {
        return path.startsWith("http") ? path : `https://www.facebook.com${path}`;
      }
    }

    const commentLinks = postEl.querySelectorAll('a[href*="comment_id"]');
    for (const link of commentLinks) {
      const href = link.getAttribute("href") || "";
      const match = href.match(/(\/groups\/\d+\/posts\/\d+\/)/);
      if (match) {
        const path = match[1];
        return href.startsWith("http") ? href.split("?")[0] : `https://www.facebook.com${path}`;
      }
    }

    return "";
  }

  function postIdFromLink(link) {
    if (!link) return "";
    const m = link.match(/\/posts\/(\d+)/);
    return m ? m[1] : "";
  }

  function extractComments(postEl) {
    const comments = [];
    const articles = postEl.querySelectorAll('div[role="article"]');

    articles.forEach((article) => {
      const ariaLabel = article.getAttribute("aria-label") || "";
      let commentTime = "";
      const zhMatch = ariaLabel.match(/^(.+?)的留言(.+)$/);
      if (zhMatch) commentTime = zhMatch[2].trim();
      else {
        const enMatch = ariaLabel.match(/(?:,\s*|·\s*)([\d\w:\s,]+(?:AM|PM)?\s*(?:ago)?)$/i);
        if (enMatch) commentTime = enMatch[1].trim();
      }

      let commentText = "";
      const textContainer = article.querySelector('div[dir="auto"]');
      if (textContainer) commentText = getVisibleText(textContainer);

      if (!commentText) return;

      comments.push({ text: commentText, time: commentTime });
    });

    return comments;
  }

  const posts = [];
  const postElements = document.querySelectorAll("div[aria-posinset]");

  postElements.forEach((postEl) => {
    const storyMsg = postEl.querySelector(
      'div[data-ad-rendering-role="story_message"]'
    );
    const postText = storyMsg ? getVisibleText(storyMsg) : "";
    const hasExpandPrompt =
      /查看更多|顯示更多|See more|Show more|View more/i.test(postText);
    if (hasExpandPrompt) return;

    const timestamp = extractTimestampFromLink(postEl);
    const postLink = extractPostLink(postEl);
    const comments = extractComments(postEl);

    const commentCountEl = Array.from(
      postEl.querySelectorAll('span[class*="xkrqix3"]')
    ).find(
      (el) =>
        /\d+則留言/.test(el.textContent) ||
        /\d+\s*comment(?:s)?/i.test(el.textContent)
    );
    const commentCount = commentCountEl
      ? parseInt(commentCountEl.textContent.match(/\d+/)?.[0] ?? "0", 10)
      : comments.length;

    if (!postText && comments.length === 0) return;

    posts.push({
      post_id: postIdFromLink(postLink),
      post_text: postText,
      timestamp,
      comment_count: commentCount,
      comments,
    });
  });

  return posts;
})();
