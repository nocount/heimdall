// Reddit: check every page. The daemon blocks Reddit by rule without
// classifying it; the post title is logged with the decision as context for
// an appeal. New Reddit navigates with pushState, which the background
// relays as "navigated" messages.

(() => {
  const POST = /^\/(?:r\/[^/]+\/|u(?:ser)?\/[^/]+\/)?comments\/[a-z0-9]+/i;
  const EXTRACT_MS = 1500;

  function postTitle() {
    const h1 = document.querySelector("shreddit-title, h1, a.title");
    const title = (h1?.getAttribute?.("title") || h1?.innerText || "").trim();
    return title ? { title } : null;
  }

  function onPage(url) {
    const isPost = POST.test(new URL(url).pathname);
    heimdall.check(url, isPost && (async () => {
      return (await heimdall.waitFor(postTitle, EXTRACT_MS)) || { title: document.title || null };
    }));
  }

  heimdall.watchUrl(onPage);
})();
