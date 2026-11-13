// YouTube players embedded in other sites' pages (youtube.com/embed/... and
// youtube-nocookie.com/embed/... iframes). Runs only in frames: a top-level
// embed URL is a normal page, handled by youtube.js and the tab redirect.
//
// A blocked embed gets a plain "blocked during focus hours" box in place of
// the player, with no appeal; the page around it is untouched. The frame is
// sent to the extension's own page (a navigation, so deleting an overlay in
// devtools gains nothing); if Firefox refuses that, the curtain stays up
// showing the same text, and keeps pausing any video that tries to play.

(() => {
  if (window === window.top) return;

  const BLOCKED_PAGE = browser.runtime.getURL("ui/embed-blocked.html");

  function block() {
    heimdall.curtain.raise();
    heimdall.curtain.el.textContent = "Blocked during focus hours";
    window.stop(); // no more of the player's page loads
    location.replace(BLOCKED_PAGE);
  }

  async function check() {
    const url = location.href;
    if (await heimdall.curtain.wanted()) heimdall.curtain.raise();
    let res = null;
    try {
      res = await browser.runtime.sendMessage({ type: "classify-frame", url });
    } catch (e) {
      console.warn("heimdall: embed report failed", e);
    }
    if (location.href !== url) return;
    // No answer at all: block if we'd be enforcing (curtain.wanted fails closed).
    const verdict = res?.verdict ?? ((await heimdall.curtain.wanted()) ? "block" : "allow");
    if (verdict === "block") {
      block();
      return;
    }
    heimdall.curtain.lower();
    // Allowed on a grant: check again when it runs out.
    if (res?.expires_at) setTimeout(check, Math.max(0, Date.parse(res.expires_at) - Date.now()) + 1000);
  }

  check();
})();
