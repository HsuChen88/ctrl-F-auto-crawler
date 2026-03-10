(() => {
  /**
   * Extract all visible FB group posts + their comments from the current DOM.
   *
   * Strategy:
   *  - Each post lives in a div[aria-posinset] (FB's virtualized list item).
   *  - Post body text is inside div[data-ad-rendering-role="story_message"].
   *  - Author name is in div[data-ad-rendering-role="profile_name"].
   *  - Timestamp can be parsed from comment aria-labels or the post's <a> links.
   *  - Comments are div[role="article"] nested inside the post container.
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
        if (
          visibleText &&
          /\d/.test(visibleText) &&
          (visibleText.includes("月") ||
            visibleText.includes("日") ||
            visibleText.includes("小時") ||
            visibleText.includes("分鐘") ||
            visibleText.includes("週") ||
            /\d+[hmd]/.test(visibleText))
        ) {
          return visibleText;
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
      const match = href.match(/\/posts\/(\d+)/);
      if (match) {
        return `https://www.facebook.com${href.split("?")[0]}`;
      }
    }

    const commentLinks = postEl.querySelectorAll('a[href*="comment_id"]');
    for (const link of commentLinks) {
      const href = link.getAttribute("href") || "";
      const match = href.match(/(\/groups\/\d+\/posts\/\d+\/)/);
      if (match) {
        return `https://www.facebook.com${match[1]}`;
      }
    }

    return "";
  }

  function extractComments(postEl) {
    const comments = [];
    const articles = postEl.querySelectorAll('div[role="article"]');

    articles.forEach((article) => {
      const ariaLabel = article.getAttribute("aria-label") || "";

      let authorName = "";
      let commentTime = "";
      const labelMatch = ariaLabel.match(/^(.+?)的留言(.+)$/);
      if (labelMatch) {
        authorName = labelMatch[1].trim();
        commentTime = labelMatch[2].trim();
      }

      if (!authorName) {
        const nameEl = article.querySelector(
          'a[role="link"][tabindex="0"] span.xzsf02u'
        );
        if (nameEl) authorName = nameEl.textContent.trim();
      }

      let commentText = "";
      const textContainer = article.querySelector('div[dir="auto"]');
      if (textContainer) {
        commentText = getVisibleText(textContainer);
      }

      if (!commentText && !authorName) return;

      comments.push({
        author: authorName,
        text: commentText,
        time: commentTime,
      });
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

    const profileName = postEl.querySelector(
      'div[data-ad-rendering-role="profile_name"]'
    );
    const author = profileName ? getVisibleText(profileName) : "";

    const timestamp = extractTimestampFromLink(postEl);
    const postLink = extractPostLink(postEl);
    const comments = extractComments(postEl);

    const commentCountEl = Array.from(
      postEl.querySelectorAll('span[class*="xkrqix3"]')
    ).find((el) => /\d+則留言/.test(el.textContent));
    const commentCount = commentCountEl
      ? commentCountEl.textContent.trim()
      : `${comments.length}則留言`;

    if (!postText && comments.length === 0) return;

    posts.push({
      author,
      post_text: postText,
      timestamp,
      post_link: postLink,
      comment_count: commentCount,
      comments,
    });
  });

  return posts;
})();
