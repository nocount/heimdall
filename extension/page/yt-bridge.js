// Runs in YouTube's page world (manifest "world": "MAIN"), where the player
// object lives. The isolated content script asks for the current player
// response with a DOM event and gets a JSON string back.
//
// getPlayerResponse() reflects in-page navigations; ytInitialPlayerResponse
// only describes the first page loaded, so it's a fallback and the content
// script checks the videoId either way.
//
// YouTube internals change: if extraction breaks, start here.

(() => {
  function playerResponse() {
    try {
      const pr = document.getElementById("movie_player")?.getPlayerResponse?.();
      if (pr?.videoDetails) return pr;
    } catch (e) {
      // fall through
    }
    return window.ytInitialPlayerResponse || null;
  }

  document.addEventListener("heimdall:get-player", (event) => {
    const nonce = event.detail;
    const pr = playerResponse();
    const vd = pr?.videoDetails;
    const data = vd
      ? {
          videoId: vd.videoId,
          title: vd.title,
          author: vd.author,
          channelId: vd.channelId,
          shortDescription: vd.shortDescription,
          keywords: vd.keywords || [],
          category: pr.microformat?.playerMicroformatRenderer?.category || null,
        }
      : null;
    document.dispatchEvent(
      new CustomEvent("heimdall:player", { detail: JSON.stringify({ nonce, data }) }),
    );
  });
})();
