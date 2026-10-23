// Shared helpers for the site content scripts (loaded first in each entry).

window.heimdall = {
  sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  },

  // Poll fn() until it returns something truthy or the timeout passes.
  async waitFor(fn, timeoutMs = 4000, intervalMs = 250) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      let value = null;
      try {
        value = fn();
      } catch (e) {
        console.warn("heimdall: extractor threw", e);
      }
      if (value) return value;
      if (Date.now() >= deadline) return null;
      await heimdall.sleep(intervalMs);
    }
  },

  // Report the page and get the daemon's verdict back via the background.
  // While enforcing, the page sits under the curtain until it's allowed; a
  // blocked page never comes out from under it because the background
  // navigates the tab to the block page.
  async check(url, extract) {
    if (await heimdall.curtain.wanted()) heimdall.curtain.raise();
    const metadata = extract ? await extract() : null;
    if (location.href !== url) return; // navigated away while extracting
    let res = null;
    try {
      res = await browser.runtime.sendMessage({ type: "classify", url, metadata });
    } catch (e) {
      console.warn("heimdall: report failed", e);
    }
    if (location.href === url && res?.verdict === "allow") heimdall.curtain.lower();
  },

  // Calls onChange(url) once per distinct URL, from any navigation signal.
  watchUrl(onChange, extraEvents = []) {
    let last = null;
    const check = () => {
      if (location.href === last) return;
      last = location.href;
      onChange(last);
    };
    browser.runtime.onMessage.addListener((msg) => {
      if (msg?.type === "navigated") check();
    });
    window.addEventListener("popstate", check);
    for (const name of extraEvents) document.addEventListener(name, check);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", check, { once: true });
    } else {
      check();
    }
  },
};

// The "checking" curtain: a full-viewport cover plus paused video while the
// daemon decides. Purely cosmetic (deleting it in devtools gains nothing),
// but it keeps a page from flashing or a video from starting before a block.
heimdall.curtain = {
  el: null,
  active: false,

  async wanted() {
    try {
      return (await browser.runtime.sendMessage({ type: "curtain?" })).curtain;
    } catch {
      return true;
    }
  },

  raise() {
    if (!this.el) {
      const el = document.createElement("div");
      el.id = "heimdall-curtain";
      el.textContent = "heimdall · checking…";
      Object.assign(el.style, {
        position: "fixed", inset: "0", zIndex: "2147483647", display: "flex",
        alignItems: "center", justifyContent: "center", background: "#0f0f0f", color: "#aaa",
        font: "15px system-ui, sans-serif", letterSpacing: "0.02em",
      });
      this.el = el;
    }
    if (!this.el.isConnected) document.documentElement.appendChild(this.el);
    this.active = true;
    document.querySelectorAll("video").forEach((v) => v.pause());
  },

  lower() {
    this.active = false;
    this.el?.remove();
  },
};

// `play` doesn't bubble, but a capturing listener on document sees it.
document.addEventListener("play", (e) => {
  if (heimdall.curtain.active) e.target.pause?.();
}, true);
