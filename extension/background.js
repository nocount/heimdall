// heimdall background: the only part of the extension that talks to the daemon.
//
// Content scripts report each page (with metadata where they can read it).
// The daemon decides; a "block" sends the tab to ui/blocked.html, which
// offers the appeal box. The curtain the content scripts draw is cosmetic:
// this redirect is the enforcement, and it happens here.
//
// Fail closed: during focus hours (per the last status the daemon sent, or
// always if there is none), an unreachable daemon blocks the page too.

const DAEMON = "http://127.0.0.1:7878";
const REQUEST_TIMEOUT_MS = 3000;
const STATUS_REFRESH_MS = 5 * 60 * 1000;
// A content script that hasn't reported a navigation by now is broken or
// gone; classify the URL without metadata, which the daemon fails closed on.
const REPORT_GRACE_MS = 8000;
const VERSION = browser.runtime.getManifest().version;
const BLOCKED_PAGE = browser.runtime.getURL("ui/blocked.html");
const FILTERED = [
  { hostSuffix: "youtube.com" },
  { hostEquals: "x.com" },
  { hostSuffix: ".x.com" },
  { hostEquals: "twitter.com" },
  { hostSuffix: ".twitter.com" },
];

class DaemonError extends Error {
  constructor(status, body) {
    super(body?.detail || `daemon HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

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
    const json = await resp.json().catch(() => null);
    if (!resp.ok) throw new DaemonError(resp.status, json);
    return json;
  } finally {
    clearTimeout(timer);
  }
}

// --- status and focus hours -------------------------------------------------

async function cachedStatus() {
  const { status } = await browser.storage.local.get("status");
  return status || null;
}

async function refreshStatus(force = false) {
  const { statusFetchedAt = 0 } = await browser.storage.local.get("statusFetchedAt");
  if (!force && Date.now() - statusFetchedAt < STATUS_REFRESH_MS) return cachedStatus();
  try {
    const status = await call("/v1/status");
    await browser.storage.local.set({ status, statusFetchedAt: Date.now() });
    if (status.next_change) {
      // Re-check open tabs the moment focus hours start (or end).
      browser.alarms.create("focus-change", { when: Date.parse(status.next_change) + 2000 });
    }
    return status;
  } catch (e) {
    console.warn("heimdall: status refresh failed", e);
    return cachedStatus();
  }
}

const DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

// Mirrors daemon/heimdall/schedule.py, for when the daemon can't be asked.
function isFocus(schedule, now = new Date()) {
  if (schedule.force_focus === true || schedule.force_focus === false) return schedule.force_focus;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: schedule.timezone, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(now).map((p) => [p.type, p.value]),
  );
  const day = parts.weekday.slice(0, 3).toLowerCase();
  const hhmm = `${parts.hour}:${parts.minute}`;
  return schedule.windows.some((w) => w.days.includes(day) && w.start <= hhmm && hhmm < w.end);
}

// Should an unanswered page be blocked right now? With no status ever seen,
// yes: failing open would make "stop the daemon" the easy way out.
function enforcing(status) {
  if (!status) return true;
  return !status.shadow && isFocus(status.schedule);
}

// --- per-tab state (survives the event page being unloaded) -----------------

async function tabStates() {
  const { tabs = {} } = await browser.storage.session.get("tabs");
  return tabs;
}

async function remember(tabId, url, metadata) {
  const tabs = await tabStates();
  tabs[tabId] = { url, metadata };
  await browser.storage.session.set({ tabs });
}

async function forget(tabId) {
  const tabs = await tabStates();
  delete tabs[tabId];
  await browser.storage.session.set({ tabs });
  browser.alarms.clear(`recheck:${tabId}`);
}

// --- badge -----------------------------------------------------------------

function setBadge(tabId, text, color, title) {
  browser.action.setBadgeText({ tabId, text }).catch(() => {});
  if (color) browser.action.setBadgeBackgroundColor({ tabId, color }).catch(() => {});
  browser.action.setTitle({ tabId, title: `heimdall: ${title}` }).catch(() => {});
}

function showVerdict(tabId, res) {
  if (res.would_verdict === "block" && res.shadow) {
    setBadge(tabId, "✕", "#c0392b", `would block: ${res.reason}`);
  } else if (res.source === "grant") {
    const until = new Date(res.expires_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    setBadge(tabId, "⏱", "#2c7be5", `granted on appeal until ${until}`);
  } else {
    setBadge(tabId, "", null, `allowed: ${res.reason}`);
  }
}

// --- classification and enforcement -----------------------------------------

function blockTab(tabId, params) {
  const url = `${BLOCKED_PAGE}?${new URLSearchParams(params)}`;
  // Replace the blocked page in history, so Back doesn't bounce into it again.
  return browser.tabs.update(tabId, { url, loadReplace: true }).catch(() => {});
}

const pendingReports = new Map(); // tabId -> {url, timer}

async function classify(url, metadata, tabId) {
  const pending = pendingReports.get(tabId);
  if (pending && pending.url === url) {
    clearTimeout(pending.timer);
    pendingReports.delete(tabId);
  }

  let res;
  try {
    res = await call("/v1/classify", { url, metadata });
  } catch (e) {
    console.warn("heimdall: classify failed", url, e);
    if (tabId !== undefined && enforcing(await cachedStatus())) {
      setBadge(tabId, "!", "#e67e22", "daemon unreachable: failing closed");
      blockTab(tabId, { u: url, x: "1" });
      return { verdict: "block", unreachable: true };
    }
    if (tabId !== undefined) setBadge(tabId, "!", "#e67e22", `daemon unreachable (${e.message || e})`);
    return { verdict: "allow", error: String(e) };
  }

  if (tabId === undefined) return res;
  showVerdict(tabId, res);
  await remember(tabId, url, metadata);
  if (res.verdict === "block") {
    blockTab(tabId, { u: url, d: res.decision_id, r: res.reason, a: res.appealable ? "1" : "0" });
  } else if (res.expires_at) {
    browser.alarms.create(`recheck:${tabId}`, { when: Date.parse(res.expires_at) + 1000 });
  }
  return res;
}

// Re-run the decision for a tab still showing the page we last judged
// (a grant ran out, or focus hours began).
async function recheck(tabId) {
  const state = (await tabStates())[tabId];
  if (!state) return;
  const tab = await browser.tabs.get(Number(tabId)).catch(() => null);
  if (!tab || tab.url !== state.url) return;
  await classify(state.url, state.metadata, Number(tabId));
}

function expectReport(tabId, url) {
  const old = pendingReports.get(tabId);
  if (old) clearTimeout(old.timer);
  const timer = setTimeout(async () => {
    pendingReports.delete(tabId);
    const tab = await browser.tabs.get(tabId).catch(() => null);
    if (tab && tab.url === url) {
      console.error("heimdall: no report from content script for", url);
      classify(url, { extraction_error: "content script never reported" }, tabId);
    }
  }, REPORT_GRACE_MS);
  pendingReports.set(tabId, { url, timer });
}

// --- messages ---------------------------------------------------------------

browser.runtime.onMessage.addListener((msg, sender) => {
  switch (msg?.type) {
    case "classify": // content script reporting its page
      return classify(msg.url, msg.metadata ?? null, sender.tab?.id);
    case "curtain?": // content script: hide the page while we decide?
      return cachedStatus().then((s) => ({ curtain: enforcing(s) }));
    case "status": // block page
      return refreshStatus(true);
    case "appeal": // block page
      return call("/v1/appeal", { decision_id: msg.decision_id, justification: msg.justification })
        .then((body) => ({ ok: true, body }))
        .catch((e) => ({ ok: false, status: e.status ?? 0, body: e.body ?? { detail: "heimdall daemon unreachable" } }));
    default:
      return undefined;
  }
});

// --- navigation -------------------------------------------------------------

browser.webNavigation.onCommitted.addListener(
  (d) => {
    if (d.frameId === 0) expectReport(d.tabId, d.url);
  },
  { url: FILTERED },
);

// SPA navigations (X's pushState, YouTube's too as a backstop). The content
// script dedupes by URL, so double notifications are harmless.
browser.webNavigation.onHistoryStateUpdated.addListener(
  (d) => {
    if (d.frameId !== 0) return;
    expectReport(d.tabId, d.url);
    browser.tabs.sendMessage(d.tabId, { type: "navigated", url: d.url }).catch(() => {});
  },
  { url: FILTERED },
);

browser.tabs.onRemoved.addListener((tabId) => forget(tabId));

// --- alarms: heartbeat, status, re-checks ------------------------------------

const HEARTBEAT_EVERY_MS = 30 * 1000;
let lastSent = 0; // this page instance; storage.session carries it across reloads

async function heartbeat() {
  lastSent = Date.now();
  try {
    await call("/v1/heartbeat", { client: "firefox", version: VERSION });
    await browser.storage.session.set({ lastHeartbeat: Date.now() });
  } catch (e) {
    console.warn("heimdall: heartbeat failed", e);
  }
}

// Firefox unloads this event page after ~30s idle and re-runs this file on
// the next event. So:
// - Only create the heartbeat alarm if it's missing. alarms.create() with an
//   existing name replaces the alarm and restarts its countdown; re-creating
//   it on every wake-up meant that while browsing (wake-ups 30-60s apart) it
//   never fired, and the watchdog killed a healthy Firefox.
// - Also heartbeat on wake-up when the last one is stale, so liveness doesn't
//   hinge on the alarm alone.
// Firefox rounds alarm periods under a minute up to one for installed
// extensions; the watchdog's heartbeat timeout allows for a minute plus a miss.
browser.alarms.get("heartbeat").then((alarm) => {
  if (!alarm) browser.alarms.create("heartbeat", { periodInMinutes: 1 });
});
browser.storage.session.get("lastHeartbeat").then(({ lastHeartbeat = 0 }) => {
  if (Date.now() - Math.max(lastSent, lastHeartbeat) > HEARTBEAT_EVERY_MS) heartbeat();
});

browser.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "heartbeat") {
    if (Date.now() - lastSent > HEARTBEAT_EVERY_MS) heartbeat();
    refreshStatus();
  } else if (alarm.name === "focus-change") {
    await refreshStatus(true);
    for (const tabId of Object.keys(await tabStates())) recheck(tabId);
  } else if (alarm.name.startsWith("recheck:")) {
    recheck(alarm.name.slice("recheck:".length));
  }
});

function boot() {
  heartbeat();
  refreshStatus(true);
}
browser.runtime.onStartup.addListener(boot);
browser.runtime.onInstalled.addListener(boot);
