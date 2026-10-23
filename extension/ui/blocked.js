// The block page. Query: u = blocked URL, d = decision id, r = reason,
// a = "1" if appealable, x = "1" if the daemon was unreachable.
// Everything from the query is shown with textContent, never as HTML.

const q = new URLSearchParams(location.search);
const target = q.get("u") || "";
// Only ever navigate back to a web page, whatever ended up in the query.
const safeTarget = /^https?:\/\//i.test(target) ? target : null;
const decisionId = Number(q.get("d")) || null;

const $ = (id) => document.getElementById(id);
let minWords = 8;
let lockTimer = null;

function shortUrl(u) {
  try {
    const url = new URL(u);
    return url.host + url.pathname + url.search;
  } catch {
    return u;
  }
}

function words(text) {
  return text.trim().split(/\s+/).filter(Boolean).length;
}

function showResult(text, kind) {
  const el = $("result");
  el.textContent = text;
  el.className = `result ${kind}`;
  el.hidden = false;
}

function updateCount() {
  const n = words($("why").value);
  $("count").textContent = `${n} / ${minWords} words`;
  $("submit").disabled = lockTimer !== null || n < minWords;
}

function lockFor(seconds, what) {
  clearInterval(lockTimer);
  const until = Date.now() + seconds * 1000;
  $("why").disabled = true;
  const tick = () => {
    const left = Math.ceil((until - Date.now()) / 1000);
    if (left <= 0) {
      clearInterval(lockTimer);
      lockTimer = null;
      $("why").disabled = false;
      $("left").textContent = "";
      updateCount();
      return;
    }
    $("left").textContent = `${what} in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  };
  lockTimer = setInterval(tick, 1000);
  tick();
  updateCount();
}

async function appeal(event) {
  event.preventDefault();
  const justification = $("why").value.trim();
  if (words(justification) < minWords) return;
  $("submit").disabled = true;
  $("submit").textContent = "Asking…";
  $("why").disabled = true;

  const res = await browser.runtime.sendMessage({ type: "appeal", decision_id: decisionId, justification });
  $("submit").textContent = "Appeal";
  $("why").disabled = false;

  if (res.ok && res.body.granted) {
    showResult(`${res.body.message} Opening it for ${res.body.minutes} minutes…`, "good");
    $("appeal").hidden = true;
    if (safeTarget) setTimeout(() => location.replace(safeTarget), 1800);
    return;
  }
  if (res.ok) {
    showResult(res.body.message, "bad");
    $("left").textContent = "";
    if (res.body.cooldown_s) lockFor(res.body.cooldown_s, "Appeal again");
    return;
  }
  showResult(res.body?.detail || "Something went wrong.", "bad");
  if (res.status === 403) $("appeal").hidden = true;
  else if (res.body?.retry_after_s) lockFor(res.body.retry_after_s, "Appeal again");
  else updateCount();
}

async function init() {
  const why = q.get("x") === "1" ? "daemon unreachable" : `decision ${decisionId}, appealable: ${q.get("a") === "1"}`;
  console.info("heimdall: block page for", target, `(${why})`);
  $("target").textContent = shortUrl(target);
  const reason = q.get("r");
  if (reason) {
    $("reason").textContent = reason;
    $("reason").hidden = false;
  }
  $("back").addEventListener("click", () => history.back());

  if (q.get("x") === "1") {
    $("title").textContent = "heimdall can't reach its daemon";
    $("unreachable").hidden = false;
    $("retry").addEventListener("click", () => safeTarget && location.replace(safeTarget));
    return;
  }
  if (q.get("a") !== "1" || !decisionId) {
    $("no-appeal").hidden = false;
    return;
  }

  $("appeal").hidden = false;
  $("appeal").addEventListener("submit", appeal);
  $("why").addEventListener("input", updateCount);
  $("why").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("appeal").requestSubmit();
  });
  updateCount();
  $("why").focus();

  const status = await browser.runtime.sendMessage({ type: "status" }).catch(() => null);
  const appeals = status?.appeals;
  if (appeals) {
    minWords = appeals.min_words;
    updateCount();
    if (!appeals.enabled) {
      $("appeal").hidden = true;
      showResult("Appeals aren't set up on this machine.", "bad");
    } else if (appeals.left_today === 0) {
      $("appeal").hidden = true;
      showResult("No appeals left today.", "bad");
    } else {
      $("left").textContent = `${appeals.left_today} left today`;
    }
  }
}

init();
