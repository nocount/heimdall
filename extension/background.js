// heimdall background: the only part of the extension that talks to the daemon.
//
// Phase 1 (shadow): report pages, show the would-be verdict as a badge, send
// heartbeats, and cache the daemon's status so Phase 2 can fail closed when
// the daemon is down. Nothing is blocked yet.

const DAEMON = "http://127.0.0.1:7878";
const REQUEST_TIMEOUT_MS = 3000;
const STATUS_REFRESH_MS = 5 * 60 * 1000;
const VERSION = browser.runtime.getManifest().version;

async function call(path, body) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetch(DAEMON + path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!resp.ok) throw new Error(`daemon HTTP ${resp.status}`);
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

// --- badge -----------------------------------------------------------------

function setBadge(tabId, text, color, title) {
  browser.action.setBadgeText({ tabId, text }).catch(() => {});
  if (color) browser.action.setBadgeBackgroundColor({ tabId, color }).catch(() => {});
  browser.action.setTitle({ tabId, title: `heimdall: ${title}` }).catch(() => {});
}

function showVerdict(tabId, res) {
  if (res.would_verdict === "block") {
    const prefix = res.shadow ? "would block" : "blocked";
    setBadge(tabId, "✕", "#c0392b", `${prefix}: ${res.reason}`);
  } else {
    setBadge(tabId, "", null, `allowed: ${res.reason}`);
  }
}

// --- classification ----------------------------------------------------------

async function classify(url, metadata, tab) {
  try {
    const res = await call("/v1/classify", { url, metadata });
    if (tab) showVerdict(tab.id, res);
    console.debug("heimdall", res.would_verdict, res.reason, url);
    return res;
  } catch (e) {
    if (tab) setBadge(tab.id, "!", "#e67e22", `daemon unreachable (${e.message || e})`);
    console.warn("heimdall: classify failed", url, e);
    return { verdict: "error", error: String(e) };
  }
}

browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.type === "classify") {
    return classify(msg.url, msg.metadata ?? null, sender.tab);
  }
  return undefined;
});

// SPA navigations (X's pushState, YouTube's too as a backstop). The content
// script dedupes by URL, so double notifications are harmless.
browser.webNavigation.onHistoryStateUpdated.addListener(
  (details) => {
    if (details.frameId !== 0) return;
    browser.tabs.sendMessage(details.tabId, { type: "navigated", url: details.url }).catch(() => {});
  },
  {
    url: [
      { hostSuffix: "youtube.com" },
      { hostEquals: "x.com" },
      { hostSuffix: ".x.com" },
      { hostEquals: "twitter.com" },
      { hostSuffix: ".twitter.com" },
    ],
  },
);

// --- heartbeat and status cache ---------------------------------------------

async function heartbeat() {
  try {
    await call("/v1/heartbeat", { client: "firefox", version: VERSION });
  } catch (e) {
    console.warn("heimdall: heartbeat failed", e);
  }
}

async function refreshStatus(force = false) {
  const { statusFetchedAt = 0 } = await browser.storage.local.get("statusFetchedAt");
  if (!force && Date.now() - statusFetchedAt < STATUS_REFRESH_MS) return;
  try {
    const status = await call("/v1/status");
    await browser.storage.local.set({ status, statusFetchedAt: Date.now() });
  } catch (e) {
    console.warn("heimdall: status refresh failed", e);
  }
}

// The event page can be unloaded when idle; alarms wake it back up.
browser.alarms.create("heartbeat", { periodInMinutes: 0.5 });
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "heartbeat") {
    heartbeat();
    refreshStatus();
  }
});

function boot() {
  heartbeat();
  refreshStatus(true);
}
browser.runtime.onStartup.addListener(boot);
browser.runtime.onInstalled.addListener(boot);
