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

  report(url, metadata) {
    return browser.runtime.sendMessage({ type: "classify", url, metadata }).catch((e) => {
      console.warn("heimdall: report failed", e);
    });
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
