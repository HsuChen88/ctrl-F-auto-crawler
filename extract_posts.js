(() => {
  /**
   * Extract all visible FB group posts + their comments from the current DOM.
   *
   * Strategy:
   *  - Each post lives in a div[aria-posinset] (FB's virtualized list item).
   *  - Post body text is inside div[data-ad-rendering-role="story_message"].
   *  - Timestamp can be parsed from comment aria-labels or the post's <a> links.
   *  - Comments are div[role="article"] nested inside the post container.
   *  - Tree (parent_comment_id): "從連結取 id + 從 DOM 找 parent"
   *    1) 從連結取 id: 每個 div[role="article"] 內 a[href*="comment_id"] 解析
   *       reply_comment_id=Y → 此則 id=Y；僅 comment_id=X → 此則 id=X。
   *    2) 從 DOM 找 parent: 最近祖先 div[role="article"] 的 id 即 parent_comment_id。
   *
   * Locale: supports 中文 (則留言、月/日/週、的留言、查看更多/顯示更多) and English
   * (comments, month names, AM/PM, "Comment by …", See more/Show more/View more).
   *
   * FB anti-scrape: timestamps use CSS reordering (position:absolute; top:3em)
   * to shuffle visible characters. We handle this by reading aria-label on
   * comment articles and the post's timestamp link.
   */

  function getVisibleText(el, options) {
    if (!el) return "";
    const opts = options || {};
    const clone = el.cloneNode(true);
    if (!opts.keepAriaHidden) {
      clone.querySelectorAll('[aria-hidden="true"]').forEach((h) => h.remove());
    }
    clone
      .querySelectorAll('[style*="position: absolute"]')
      .forEach((h) => h.remove());
    return (clone.textContent || "").trim().replace(/\s+/g, " ");
  }

  /** Comment body with emoji preserved (FB puts emoji in aria-hidden nodes). */
  function getCommentBodyText(el) {
    return getVisibleText(el, { keepAriaHidden: true });
  }

  /** Prefer timestamp from the link's aria-labelledby target (correct order; FB shuffles chars with CSS). */
  function extractTimestampFromLink(postEl) {
    const links = postEl.querySelectorAll("a[href]");
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      if (!href.includes("/posts/") && !href.includes("permalink") && !href.includes("multi_permalinks")) continue;

      const labelledBy = link.querySelector("[aria-labelledby]");
      const labelIds = (labelledBy?.getAttribute("aria-labelledby") || "").trim().split(/\s+/);
      for (const id of labelIds) {
        const labelEl = document.getElementById(id);
        if (labelEl) {
          const t = (labelEl.textContent || "").trim();
          if (t && /\d/.test(t)) return t;
        }
      }

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

  /**
   * Parse comment_id and reply_comment_id from any link in the article.
   * - reply_comment_id=Y → this comment's id is Y (reply).
   * - comment_id=X only → this comment's id is X (root-level).
   * Returns { commentId, replyCommentId } (replyCommentId set only for replies).
   */
  function idsFromArticle(article) {
    const links = article.querySelectorAll('a[href*="comment_id"], a[href*="reply_comment_id"]');
    for (const a of links) {
      const href = a.getAttribute("href") || "";
      const reply = href.match(/reply_comment_id=(\d+)/);
      const top = href.match(/comment_id=(\d+)/);
      if (reply) {
        return { commentId: top ? top[1] : "", replyCommentId: reply[1] };
      }
      if (top) {
        return { commentId: top[1], replyCommentId: "" };
      }
    }
    const m2 = article.querySelector('a[href*="/comments/"]');
    if (m2) {
      const h = (m2.getAttribute("href") || "").match(/\/comments\/(\d+)/);
      if (h) return { commentId: h[1], replyCommentId: "" };
    }
    return { commentId: "", replyCommentId: "" };
  }

  /** This comment's canonical id: reply_comment_id if present, else comment_id. */
  function commentIdFromArticle(article) {
    const { commentId, replyCommentId } = idsFromArticle(article);
    return replyCommentId || commentId || "";
  }

  /** Closest ancestor that is a comment article; null if none (root comment). */
  function parentArticle(article) {
    let el = article.parentElement;
    while (el) {
      if (el.getAttribute("role") === "article") return el;
      el = el.parentElement;
    }
    return null;
  }

  function authorFromAriaLabel(ariaLabel) {
    const raw = ariaLabel || "";
    const replyZh = raw.match(/^(.+?)回覆/);
    if (replyZh) return replyZh[1].trim();
    const topZh = raw.match(/^(.+?)的留言/);
    if (topZh) return topZh[1].trim();
    const en = raw.match(/^Comment by\s+(.+?)(?:\s+·|$)/i);
    if (en) return en[1].trim();
    const replyEn = raw.match(/^(.+?)\s+replied\s+to/i);
    if (replyEn) return replyEn[1].trim();
    return "";
  }

  function timeFromAriaLabel(ariaLabel) {
    const raw = ariaLabel || "";
    const afterReply = raw.match(/的回覆(.+)$/);
    if (afterReply) return afterReply[1].trim();
    const afterComment = raw.match(/的留言(.+)$/);
    if (afterComment) return afterComment[1].trim();
    const en = raw.match(/(?:,\s*|·\s*)([\d\w:\s,]+(?:AM|PM)?\s*(?:ago)?)$/i);
    if (en) return en[1].trim();
    return "";
  }

  /**
   * Parent from DOM: id of the closest ancestor div[role="article"].
   * Link-derived id: reply_comment_id if present, else comment_id.
   */
  function parentCommentIdFromDom(article) {
    const parent = parentArticle(article);
    if (!parent) return null;
    const id = commentIdFromArticle(parent);
    return id || null;
  }

  function extractComments(postEl, postId) {
    const comments = [];
    const articles = postEl.querySelectorAll('div[role="article"]');

    articles.forEach((article) => {
      const ariaLabel = article.getAttribute("aria-label") || "";
      const commentTime = timeFromAriaLabel(ariaLabel);

      let commentText = "";
      const textContainer = article.querySelector('div[dir="auto"]');
      if (textContainer) commentText = getCommentBodyText(textContainer);

      if (!commentText) return;

      const { commentId, replyCommentId } = idsFromArticle(article);
      const thisCommentId = replyCommentId || commentId || "";
      const author = authorFromAriaLabel(ariaLabel);
      const parentCommentId = parentCommentIdFromDom(article);

      comments.push({
        text: commentText,
        time: commentTime,
        comment_id: thisCommentId || undefined,
        author: author || undefined,
        parent_comment_id: parentCommentId != null ? parentCommentId : null
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
    const hasExpandPrompt =
      /查看更多|顯示更多|See more|Show more|View more/i.test(postText);
    if (hasExpandPrompt) return;

    const timestamp = extractTimestampFromLink(postEl);
    const postLink = extractPostLink(postEl);
    const pid = postIdFromLink(postLink);
    const comments = extractComments(postEl, pid);

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
      post_id: pid,
      post_text: postText,
      timestamp,
      comment_count: commentCount,
      comments,
    });
  });

  return posts;
})();
