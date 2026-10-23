// YouTube: report every page; on /watch, include the video's metadata read
// from the player via page/yt-bridge.js.

(() => {
  const PLAYER_REPLY_MS = 500;

  function askBridge() {
    const nonce = Math.random().toString(36).slice(2);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        document.removeEventListener("heimdall:player", onReply);
        resolve(null);
      }, PLAYER_REPLY_MS);
      function onReply(event) {
        let reply;
        try {
          reply = JSON.parse(event.detail);
        } catch {
          return;
        }
        if (reply.nonce !== nonce) return;
        clearTimeout(timer);
        document.removeEventListener("heimdall:player", onReply);
        resolve(reply.data);
      }
      document.addEventListener("heimdall:player", onReply);
      document.dispatchEvent(new CustomEvent("heimdall:get-player", { detail: nonce }));
    });
  }

  // Poll until the player describes the video in the URL. Right after an
  // in-page navigation it can still hold the previous video for a moment.
  async function watchMetadata(videoId) {
    const deadline = Date.now() + 4000;
    while (Date.now() < deadline) {
      const d = await askBridge();
      if (d && d.videoId === videoId) {
        return {
          video_id: d.videoId,
          title: d.title,
          channel: d.author,
          channel_id: d.channelId,
          description: d.shortDescription,
          tags: d.keywords,
          category: d.category,
        };
      }
      await heimdall.sleep(250);
    }
    return null;
  }

  async function onPage(url) {
    const u = new URL(url);
    if (u.pathname !== "/watch") {
      heimdall.report(url, null);
      return;
    }
    const videoId = u.searchParams.get("v");
    const metadata = videoId ? await watchMetadata(videoId) : null;
    if (location.href !== url) return; // navigated away while extracting
    if (!metadata) console.error("heimdall: could not read player metadata for", videoId);
    heimdall.report(url, metadata || { extraction_error: "player response unavailable" });
  }

  // yt-navigate-finish fires on first load and after every in-page navigation.
  heimdall.watchUrl(onPage, ["yt-navigate-finish"]);
})();
