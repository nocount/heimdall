// X: check every page; on single posts and profiles, include their text. The
// daemon blocks X by rule without classifying it; the text is logged with the
// decision as context for an appeal. X's markup changes often: every selector
// lives in SEL.

(() => {
  const SEL = {
    article: 'article[data-testid="tweet"]',
    tweetText: '[data-testid="tweetText"]',
    userName: '[data-testid="User-Name"]',
    photo: '[data-testid="tweetPhoto"] img[alt]',
    profileBio: '[data-testid="UserDescription"]',
    profileName: '[data-testid="UserName"]',
    timelineText: '[data-testid="primaryColumn"] article [data-testid="tweetText"]',
  };

  const STATUS = /^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)/;
  const NOT_PROFILES = new Set([
    "home", "explore", "notifications", "messages", "i", "settings", "search",
    "compose", "login", "logout", "signup", "tos", "privacy", "jobs", "hashtag",
    "communities", "premium", "account", "intent", "share",
  ]);

  const text = (el) => (el?.innerText || "").trim();

  // On a thread page, parent posts render above the focal one; pick the
  // article whose timestamp links to this status ID.
  function focalArticle(id) {
    const articles = [...document.querySelectorAll(SEL.article)];
    return articles.find((a) => a.querySelector(`a[href$="/status/${id}"] time`)) || null;
  }

  function extractStatus(id) {
    const article = focalArticle(id);
    if (!article) return null;
    const texts = article.querySelectorAll(SEL.tweetText);
    const body = text(texts[0]);
    const alts = [...article.querySelectorAll(SEL.photo)].map((img) => img.alt).filter((a) => a && a !== "Image");
    if (!body && !alts.length) return null;
    return {
      author: (text(article.querySelector(SEL.userName)).match(/@\w+/) || [null])[0],
      text: body || `[image] ${alts.join(" / ")}`,
      quoted_text: text(texts[1]) || null,
    };
  }

  function extractProfile(handle) {
    const bio = text(document.querySelector(SEL.profileBio));
    const recent = [...document.querySelectorAll(SEL.timelineText)].slice(0, 5).map(text).filter(Boolean);
    if (!bio && !recent.length) return null;
    return {
      handle,
      name: text(document.querySelector(SEL.profileName)).split("\n")[0] || null,
      bio: bio || null,
      recent,
    };
  }

  // X is blocked by rule, so this text only serves as context for an appeal.
  // Wait briefly for it: the page sits under the curtain meanwhile.
  const EXTRACT_MS = 2000;

  async function onPage(url) {
    const path = new URL(url).pathname;
    const status = path.match(STATUS);
    const first = path.split("/")[1] || "";
    let kind = null;
    let extract = null;
    if (status) {
      kind = "status";
      extract = () => extractStatus(status[2]);
    } else if (first && !NOT_PROFILES.has(first.toLowerCase()) && /^[A-Za-z0-9_]{1,15}$/.test(first)) {
      kind = "profile";
      extract = () => extractProfile(first);
    }
    heimdall.check(url, extract && (async () => {
      const metadata = await heimdall.waitFor(extract, EXTRACT_MS);
      return metadata || { extraction_error: `${kind} content not found` };
    }));
  }

  heimdall.watchUrl(onPage);
})();
