# heimdall — Implementation Plan

A smart, content-aware distraction filter for Firefox on Ubuntu. It allows educational YouTube and AI-related X content during focus hours, blocks the rest, and lets you plead your case in one sentence.

Status: **plan, not yet built**. Target machine: the Ubuntu desktop (Ryzen 7800X3D, RTX 5070 Ti, Firefox snap).

---

## 1. Goals and non-goals

### Goals
- **Smart filtering instead of blunt blocking.** Each page is judged on its content: title, channel, description, tags, category, or tweet text. A domain name alone doesn't decide.
- **Bypass resistance at level (b).**
  - Casual workarounds stop working: private windows, new profiles, disabling the extension, safe mode.
  - Undoing heimdall requires `sudo` and deliberate effort.
- **Justify-to-continue.**
  - Blocked pages offer a one-sentence appeal, judged by a stronger model.
  - Typing the sentence is the friction.
- **Light on the local machine.**
  - Both models are hosted, so there is no persistent GPU load.
  - A local fallback exists for the classifier if ever needed.
- **Observability.** Every decision and appeal is logged, which makes tuning possible.

### Non-goals (for the POC)
- Other browsers: they get killed, not filtered.
- Phone.
- Desktop apps.
- Per-tweet filtering inside the X timeline.
- Defeating a determined adversary who has `sudo`. That adversary is you, and this is a speed bump, not a vault.

---

## 2. Key decisions (and why)

| Decision | Choice | Reasoning |
|---|---|---|
| Interception point | Firefox extension | You only use Firefox. An extension sees the page content (needed for content-aware judgments) and runs in private windows when forced by policy. A system proxy can't see HTTPS content without MITM. |
| Bypass resistance | Firefox enterprise policy + root watchdog | `policies.json` force-installs the extension, so it can't be removed. A root watchdog covers safe mode and other browsers. |
| Classifier | **Jev** (`typesafe/jev-1.13` via OpenRouter), with Clef-flash on Workers AI as fallback. *Originally Clef-flash; changed after the Phase 0 bake-off: Jev kept 7/10 allow labels vs Clef's 5/10 at zero false allows, and its p99 (976ms) fits the timeout where Clef's (2.1s) doesn't.* Original reasoning for Clef: | ~40ms median (vendor-reported), so the "checking" curtain is near-invisible. Accepts images (thumbnails later). Open weights under Apache 2.0, with a local fallback via llama.cpp. Well within the Workers AI free tier. Final choice gated on the Phase 0 bake-off against Jev and Microsoft-Decision-1. |
| Appeal judge | **Claude Haiku 5.5** (`claude-haiku-5-5`) | Already set up. Appeals are rare and need real reasoning. ~$0.0003 per appeal. |
| Decision logic | Atomic questions to the model; rules in code | Decision models work best on small typed questions. Code combines them, so rules stay tunable and auditable without re-prompting. |
| Failure policy | **Fail closed** during focus hours | Otherwise "stop the daemon" or "lose network" becomes the easy out. |
| Block mechanism | Redirect to an extension page, not a DOM overlay | An overlay can be deleted via devtools in seconds. A redirect can't, so devtools stay available for your dev work. |
| Focus hours | **Mon–Fri 09:00–17:00 America/Los_Angeles** | Your "9-5". *Assumption:* weekdays only. Change it in config. |
| Daemon language | Python 3.12 + FastAPI | Fast to build; matches your stack. |

---

## 3. Architecture

```
┌──────────────────────── Firefox (snap) ────────────────────────┐
│  heimdall extension (force-installed, runs in private windows) │
│   ├─ content scripts: youtube.js, x.js  → extract metadata     │
│   ├─ background.js → POST 127.0.0.1:7878, redirects, heartbeat │
│   └─ blocked.html  → reason + one-sentence appeal box          │
└───────────────────────────────┬────────────────────────────────┘
                                │ HTTP (localhost only)
┌───────────────────────────────▼────────────────────────────────┐
│  heimdall daemon (systemd, user `heimdall`, config root-owned) │
│   ├─ schedule  → is it focus time?                             │
│   ├─ policy    → page-type rules, grants, cache, thresholds    │
│   ├─ classify  → Clef-flash via /v1/systemone (Workers AI)     │
│   ├─ appeal    → Claude Haiku 5.5 (Anthropic API)              │
│   └─ SQLite    → decisions, appeals, grants, heartbeats        │
└───────────────────────────────┬────────────────────────────────┘
                                │ reads heartbeat state
┌───────────────────────────────▼────────────────────────────────┐
│  heimdall-watchdog (systemd, root)                             │
│   ├─ Firefox running + no heartbeat > 60s in focus hours → kill│
│   └─ other browsers (chromium, chrome, brave…) in focus → kill │
└────────────────────────────────────────────────────────────────┘
```

---

## 4. Request flow (a YouTube video)

1. You navigate to `youtube.com/watch?v=…`. This can be a full load, or YouTube's in-page navigation (`yt-navigate-finish` event).
2. `youtube.js` pauses the video and shows a "checking" curtain. It then reads the player response via a small page-world bridge:
   - `videoDetails`: title, author, shortDescription, keywords
   - `microformat`: category
3. The background script POSTs to `heimdall /v1/classify` with `{url, site, page_type, metadata}`.
4. The daemon runs the policy pipeline (section 6). It returns `{verdict, reason, category, probs, ttl}`.
5. If the verdict is **allow**, the curtain lifts and the video resumes.
6. If the verdict is **block**, the tab is redirected to `blocked.html?id=<decision_id>`. The page shows the reason and an appeal box.
7. If you submit an appeal, it goes to `/v1/appeal`. Haiku 5.5 decides grant or deny, plus a duration.
   - **Grant:** a URL-scoped grant is stored and the tab returns to the original URL.
   - **Deny:** a short explanation is shown and a cooldown starts.

**X flow:** the same, except `x.js` extracts the tweet text, author, quoted text, and the profile bio on profile pages. In-page navigation is caught via `webNavigation.onHistoryStateUpdated`. Feed pages are blocked by rule, with no model call.

---

## 5. Components in detail

### 5.1 Firefox extension (`extension/`)
- **Manifest.**
  - Manifest V3, with `browser_specific_settings.gecko.id = "heimdall@<yourdomain-or-handle>"`. The ID must stay stable for signing and policy.
  - Permissions: `tabs`, `webNavigation`, `storage`.
  - Host permissions: `*://*.youtube.com/*`, `*://x.com/*`, `*://twitter.com/*`, `http://127.0.0.1:7878/*`.
  - Note: as of Firefox 153, users can't change host permissions on force-installed MV3 extensions.
- **`content/youtube.js`.** Handles `/watch`, `/shorts`, the home feed, and search.
  - It injects `page/yt-bridge.js` into the page world to call `document.getElementById('movie_player').getPlayerResponse()`.
  - This avoids the stale `ytInitialPlayerResponse` problem after in-page navigation.
  - *Verify field paths in Phase 1; YouTube internals change.*
- **`content/x.js`.** Extracts `[data-testid="tweetText"]`, the author handle, and the profile bio. *Selectors are brittle; isolate them in one file.*
- **`background.js`.**
  - Talks to the daemon and redirects blocked tabs.
  - Caches the focus schedule, so it can still fail closed when the daemon is down.
  - Sends a heartbeat every 30s.
- **`ui/blocked.html` + `blocked.js`.**
  - Shows the reason, with the category and probability.
  - The appeal textarea has a minimum length (8 words) and is disabled during cooldown.
  - Also shows grant and deny messages.
- **Curtain.** A full-viewport overlay plus `video.pause()` while waiting.
  - It is purely cosmetic: removing it via devtools gains nothing, because the redirect still happens on block.
  - It has a hard timeout: if there's no answer within 3s, the extension fails closed and redirects with "heimdall unreachable".

### 5.2 Daemon (`daemon/heimdall/`)
- FastAPI + uvicorn, bound to `127.0.0.1:7878` only.
- Runs as system user `heimdall` under systemd.
- **Endpoints:**
  - `POST /v1/classify`: page metadata in, verdict out.
  - `POST /v1/appeal`: `{decision_id, justification}` in, `{granted, minutes, message}` out.
  - `POST /v1/heartbeat`: called by the extension every 30s.
  - `GET /v1/status`: focus state, schedule, model health. Used by the extension and for debugging.
- **Modules:**
  - `schedule.py`: focus windows and timezone.
  - `policy.py`: the pipeline in section 6.
  - `models/systemone.py`: a provider-agnostic `/v1/systemone` client with three providers.
    - Workers AI (Clef-flash)
    - OpenRouter (Jev, Microsoft-Decision-1), for the bench
    - local llama.cpp (fallback)
  - `models/appeals.py`: the Anthropic client for Haiku 5.5.
  - `cache.py`: verdict cache keyed by video ID or tweet ID.
  - `db.py`: the SQLite store.
- **Storage.** `/var/lib/heimdall/heimdall.db`, with tables:
  - `decisions`: ts, url, site, page_type, metadata JSON, model, probs JSON, verdict, latency_ms, reason
  - `appeals`: ts, decision_id, justification, model output, granted, minutes
  - `grants`: url, expires_at
  - `heartbeats`: last_seen
- **Config.** `/etc/heimdall/config.yaml`, owned `root:heimdall`, mode `0640`.
- **Secrets.** `/etc/heimdall/secrets.env`, mode `0600`, loaded via `EnvironmentFile=`.
  - `CF_ACCOUNT_ID`, `CF_API_TOKEN`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` (bench only)

### 5.3 Watchdog (`deploy/watchdog.py`)
- A root systemd service that polls every 15s.
- **During focus hours:**
  - If any process named `firefox` is running and there's been no heartbeat for over 60s, it terminates Firefox. This covers safe mode and a crashed or disabled extension.
  - It also terminates `chromium`, `chrome`, `google-chrome`, `brave`, `vivaldi`, `opera`, `epiphany`, and flatpak browser processes. The list lives in config.
- A grace period of 90s after Firefox starts, so the extension has time to boot.
- Firefox's session restore brings tabs back, so a kill is annoying rather than destructive.

### 5.4 Lockdown (`deploy/policies.json`)
Install at `/etc/firefox/policies/policies.json`. The snap reads this path. Put the signed XPI in the same directory, because the snap may not be able to read file URLs elsewhere.

```json
{
  "policies": {
    "ExtensionSettings": {
      "heimdall@<handle>": {
        "installation_mode": "force_installed",
        "install_url": "file:///etc/firefox/policies/heimdall.xpi",
        "private_browsing": true,
        "updates_disabled": true
      }
    },
    "DisableSafeMode": true,
    "BlockAboutProfiles": true
  }
}
```

- `force_installed` means the extension can't be removed or disabled.
- `private_browsing: true` (Firefox 136+) makes it run in private windows.
- Policies apply to every profile, so a new profile doesn't escape.
- `DisableSafeMode` removes the menu entry. The Shift-key launch can only be blocked via Windows Group Policy, which is why the watchdog exists.
- Optional: `BlockAboutConfig`. It costs you about:config and gains little, so it's left off by default.
- Signing: `web-ext sign --channel unlisted` with your AMO API key and secret. The result is signed but not published. Every update needs a version bump and a re-sign.

---

## 6. Policy pipeline (`policy.py`)

Evaluated in order. The first match wins.

1. **Outside focus window → allow.** The decision is still logged.
2. **Active grant for this exact URL → allow.**
3. **Static page-type rules (no model call):**
   - YouTube home feed (`/`), Shorts (`/shorts/*`), and `/feed/*` → **block**
   - YouTube search results, channel pages, `/watch` → go to the model (watch) or allow (search, channel)
   - X home timeline (`/home`), `/explore`, `/notifications`, and lists → **block**
   - X single tweet (`/<user>/status/<id>`), profile, search → go to the model
   - Optional hard-block domains (e.g. reddit) → **block**
   - Always-allow domains → **allow**
4. **Cache hit** (video ID or tweet ID within its TTL) → reuse the verdict.
5. **Model call** with the atomic questions (section 7). Then the code applies the rules:
   - YouTube allow requires `P(educational) ≥ θ_edu`. `content_type` only names the category in the block reason.
     - *Changed after Phase 0.* The original rule also required `content_type ∈ {tutorial_or_lecture, technical_talk}` and `confidence ≥ θ_conf`. That blocked 8 of 10 allow labels, which were AI interviews and podcasts that the models correctly call commentary or news. Clef's `confidence` also isn't P(choice): a tutorial at P = 0.95 had confidence 0.54.
   - X: not classified. *Changed after Phase 0:* X is in `hard_block_domains`, so every X page is blocked during focus hours, and a granted appeal opens one post, profile or search.
6. **Model error or timeout → block.** The block is appealable, and the reason is marked "classifier unavailable".

The thresholds (`θ`) are set from Phase 0 data, not guessed. Each classifier provider has its own, because the models aren't calibrated alike. Jev: `θ_edu = 0.81`; Clef (fallback): `θ_edu = 0.76`.

---

## 7. Classifier request

Illustrative. The exact field names differ between Workers AI and OpenRouter, so they get verified in Phase 0.

```json
{
  "model": "clef-flash",
  "state": {
    "site": "youtube",
    "title": "Building a Transformer from Scratch — Lecture 4",
    "channel": "Some University",
    "category": "Education",
    "tags": ["machine learning", "pytorch"],
    "description": "<first ~1500 chars>"
  },
  "questions": {
    "content_type": {
      "type": "choice",
      "question": "What kind of video is this?",
      "options": ["tutorial_or_lecture", "technical_talk", "news", "commentary_or_opinion",
                  "entertainment", "gaming", "music", "vlog", "other"]
    },
    "educational": {
      "type": "noul",
      "question": "Would watching this primarily teach a skill or substantive knowledge, rather than entertain?"
    },
    "clickbait_mismatch": {
      "type": "noul",
      "question": "Does the title promise something educational that the description suggests it is not?"
    }
  }
}
```

The X variant asks `ai_related` (noul: "Is this primarily about AI, ML, or LLMs?") and `content_type` (choice: technical, news, opinion, meme/joke, personal, other).

**Why atomic questions:** decision models return probabilities, not reasons. The `content_type` answer doubles as the human-readable block reason, e.g. "classified as commentary (87%)". Asking several questions about one page costs about the same as one question, because the page is shared state.

**Thumbnail (later experiment):** Clef-flash accepts images. Send the thumbnail bytes as a second signal against clickbait. Only add it if Phase 1 data shows title and description aren't enough.

---

## 8. Appeal flow

- **Input to Haiku 5.5:**
  - your focus rules (from config)
  - the page metadata
  - the classifier output
  - your justification
  - today's appeal history: count, plus previous justifications
- **Instructions:** a system prompt asking it to judge whether the justification is specific and plausible. Examples:
  - "my friend sent me this video about X for a project we're doing" is specific.
  - "I just want to watch it" is not.
  - It should be skeptical of vague or repeated excuses.
- **Output:** structured, via tool or structured output: `{grant: bool, minutes: 10|20|40, message: string}`. `message` is shown to you either way.
- **Limits, enforced in code, not by the model:**
  - Minimum 8 words.
  - Maximum 5 appeals per day.
  - 10-minute cooldown on the same URL after a denial.
  - Grants are scoped to one URL (or video ID), never a domain.
  - Maximum grant 40 minutes.
- Everything is logged. A weekly review of appeals is the best tuning signal for the rules.

---

## 9. Config (`/etc/heimdall/config.yaml`)

```yaml
timezone: America/Los_Angeles
focus_windows:
  - days: [mon, tue, wed, thu, fri]
    start: "09:00"
    end: "17:00"

classifier:
  provider: workers_ai          # workers_ai | openrouter | local_llamacpp
  model: "@cf/cloudflare/clef-flash"
  timeout_ms: 1500
  thresholds: { educational: 0.75, content_conf: 0.6, ai_related: 0.7 }
  fallback:
    provider: local_llamacpp    # optional; requires local GGUF + llama-server
    url: http://127.0.0.1:8081

appeals:
  model: claude-haiku-5-5
  min_words: 8
  max_per_day: 5
  deny_cooldown_min: 10
  max_grant_min: 40

rules:
  youtube:
    allow_types: [tutorial_or_lecture, technical_talk]
    block_paths: ["/", "/shorts/*", "/feed/*"]
  x:
    block_paths: ["/home", "/explore", "/notifications", "/i/lists/*"]
  hard_block_domains: []        # e.g. [reddit.com]
  always_allow_domains: []

cache_ttl_days: { youtube: 30, x: 1 }

watchdog:
  heartbeat_timeout_s: 60
  startup_grace_s: 90
  kill_browsers: [chromium, chrome, google-chrome, brave, vivaldi, opera, epiphany]
```

---

## 10. Phased task list

### Phase 0: Classifier bake-off (offline)
**Goal:** pick the classifier and thresholds from your own data.

- [ ] Create a Cloudflare account. Get the account ID and an API token with Workers AI scope.
- [ ] Create an OpenRouter key, for comparing Jev and Microsoft-Decision-1.
- [ ] `bench/export_history.py`: copy `places.sqlite` from `~/snap/firefox/common/.mozilla/firefox/<profile>/` (copy it, because Firefox locks the live file). Extract YouTube `/watch` URLs from the last ~60 days.
- [ ] `bench/fetch_meta.py`: fetch title, channel, description, tags, and category per video via `yt-dlp --dump-json --skip-download`.
- [ ] `bench/label.py`: a terminal labeler that shows the metadata and takes `a`/`b`/`s` (allow/block/skip). Target about 80 YouTube labels, with a deliberate mix.
- [ ] X samples: paste about 20 tweets you'd allow or block into a CSV. X needs login, so offline scraping isn't worth it here. X tuning happens mostly in Phase 1.
- [ ] `bench/run_bench.py`: run every labeled item through Clef-flash (Workers AI), Jev (OpenRouter) and Microsoft-Decision-1 (OpenRouter) with identical questions, then report:
  - accuracy against your labels
  - **false-allow rate**, which matters most: a leak defeats the tool
  - false-block rate: friction, but appealable
  - a threshold sweep
  - p50 and p95 latency including network
- [ ] Optional: run the Clef-flash Q4_K_M GGUF via llama.cpp once, to confirm the local fallback works on your GPU.
- **Gate:**
  - at least 90% agreement
  - false-allow rate ≤ 5% at the chosen thresholds
  - p95 under 300ms
  - Pick the model and write the thresholds into config.

### Phase 1: Daemon + extension in shadow mode
**Goal:** real-world data with no enforcement yet.

- [ ] Scaffold the repo (section 11), using `uv` for Python deps.
- [ ] Daemon:
  - [ ] `schedule.py`
  - [ ] `policy.py`
  - [ ] `systemone.py` (all three providers)
  - [ ] `cache.py`
  - [ ] `db.py`
  - [ ] the endpoints
  - [ ] a `shadow: true` flag that always returns allow but logs the would-be verdict
- [ ] Extension:
  - [ ] YouTube and X content scripts
  - [ ] the page-world bridge
  - [ ] in-page navigation handling
  - [ ] the background script with the daemon client and heartbeat
- [ ] Run the daemon in the foreground and the extension via `web-ext run` in a dev profile.
- [ ] `heimdall review`: a CLI that lists the last N would-be blocks and allows, so you can mark mistakes. Corrections are appended to the labeled set.
- [ ] Use normally for 2–3 days, then re-tune the thresholds and page rules.
- **Gate:** the would-be false-block rate on real browsing is low enough that you'd tolerate it.

### Phase 2: Enforcement
- [ ] The curtain (pause, overlay, 3s timeout).
- [ ] The redirect to `blocked.html`, with the reason display.
- [ ] Appeal UI, the `/v1/appeal` endpoint, the Haiku 5.5 integration with structured output, and the limits and cooldowns.
- [ ] URL-scoped grants with expiry. The extension re-checks when a grant expires on an open tab.
- [ ] Fail-closed behavior when the daemon is unreachable, using the cached schedule.
- [ ] Turn off shadow mode. Use it for about a week.
- **Gate:** you can work a full 9–5 with it on without wanting to rip it out. All appeals are reviewed.

### Phase 3: Lockdown
- [ ] Create the AMO account and generate API credentials.
- [ ] `web-ext sign --channel unlisted`, then copy the XPI to `/etc/firefox/policies/heimdall.xpi`.
- [ ] Install `policies.json`. Check `about:policies` shows it active and that the extension can't be disabled in `about:addons`.
- [ ] `deploy/install.sh`:
  - [ ] create the `heimdall` system user
  - [ ] create `/etc/heimdall` and `/var/lib/heimdall` with the correct ownership
  - [ ] install the systemd units: `heimdall.service` and `heimdall-watchdog.service`
- [ ] Watchdog: heartbeat check, browser kill list, startup grace.
- [ ] **Bypass test checklist** (all should fail during focus hours):
  - [ ] private window
  - [ ] new profile (`firefox -P`)
  - [ ] disable or remove the extension
  - [ ] safe mode (menu and Shift-launch)
  - [ ] stop the daemon without sudo
  - [ ] install and launch Chromium via `flatpak --user`
  - [ ] devtools: delete the curtain, edit the block page
  - [ ] `youtube-nocookie.com` embed on another site *(expected to leak; see section 12)*

### Later (post-POC)
- Per-tweet filtering in the X timeline: batch-classify visible tweets and hide the non-AI ones.
- Thumbnails as an image signal.
- Transcript snippets, if descriptions prove insufficient.
- A weekly stats page: time allowed vs. blocked, appeal patterns.
- **Distill to CPU:** once a few thousand logged decisions exist, fine-tune a small encoder model (e.g. Laya, 421M) on your own labels. That would run on CPU with no GPU or API at all.
- Desktop app monitoring. This is hard on Wayland and likely needs a GNOME Shell extension to see the focused window.
- Extending focus windows to evenings or weekends.

---

## 11. Repo layout

```
heimdall/
├── PLAN.md                     # this document
├── config.example.yaml
├── daemon/
│   ├── pyproject.toml
│   └── heimdall/
│       ├── app.py              # FastAPI app + endpoints
│       ├── schedule.py
│       ├── policy.py
│       ├── cache.py
│       ├── db.py
│       ├── cli.py              # `heimdall review`, `heimdall status`
│       └── models/
│           ├── systemone.py    # Workers AI / OpenRouter / llama.cpp
│           └── appeals.py      # Anthropic Haiku 5.5
├── extension/
│   ├── manifest.json
│   ├── background.js
│   ├── content/youtube.js
│   ├── content/x.js
│   ├── page/yt-bridge.js
│   └── ui/{blocked.html, blocked.js, curtain.css}
├── bench/
│   ├── export_history.py
│   ├── fetch_meta.py
│   ├── label.py
│   ├── run_bench.py
│   └── data/                   # labels.csv (gitignored)
└── deploy/
    ├── install.sh
    ├── heimdall.service
    ├── heimdall-watchdog.service
    ├── watchdog.py
    └── policies.json
```

---

## 12. Failure modes and known holes

| Risk | Impact | Mitigation |
|---|---|---|
| Decision-model category is ~5 weeks old; prices, context limits and APIs are shifting (Clef-flash's hosted context was cut to 24,576 tokens on Oct 9) | Integration breaks or costs change | Provider-agnostic `/v1/systemone` client; model is a config value; local llama.cpp fallback |
| Vendor benchmarks don't transfer; calibration drifts on your distribution | Wrong thresholds → leaks or friction | Phase 0 on your own labels; Phase 1 shadow mode; ongoing `heimdall review` |
| Clef-flash leans toward "act" when info is thin (19/50 vs Jev's 6/50 in one independent test) | Possible false-allows on sparse metadata | Require *both* a type match and P(educational) above threshold; low confidence → block |
| Workers AI or Anthropic outage | Everything blocks during focus hours | Fail-closed by design; optional local fallback |
| YouTube/X DOM or player internals change | Metadata extraction breaks → blocks (fail closed) | Selectors isolated per site; extraction errors logged loudly |
| Browsing metadata sent to Cloudflare and Anthropic | Privacy | Only titles/descriptions/tweet text, no cookies or history dumps; local Clef-flash if it bothers you |
| `sudo` user can undo everything | Determined bypass | Accepted for level (b). Level (c) would need someone else to hold an admin password. |
| Phone, other devices | Full bypass | Out of scope |
| YouTube embeds on other sites, alternative frontends (Invidious etc.) | Leak | Add embed-domain and frontend-domain rules once noticed in logs |
| Renamed browser binary to dodge the watchdog kill list | Leak | Accepted; deliberate effort is the line |
| Watchdog kills Firefox mid-task | Lost unsaved form state | Only triggers on missing heartbeat; session restore recovers tabs |

---

## 13. Cost estimate

Assumes a few hundred classifications per weekday, under 1k tokens each, and a handful of appeals.

| Item | Rate | Expected monthly |
|---|---|---|
| Clef-flash (Workers AI) | $0.038/M input, output free; 10k free neurons/day | **$0**: well inside the free allocation |
| Haiku 5.5 appeals | $0.10/M in, $0.50/M out (prompts ≤100k) | **< $0.10** |
| Phase 0 bench (3 models × ~100 items) | — | **< $0.05** |

---

## 14. Open items / assumptions to confirm
- Focus hours are **weekdays only**. Say so if 9–5 should include weekends.
- YouTube search and channel pages are **allowed** during focus hours; only watch pages are judged. The alternative is blocking browsing surfaces entirely.
- The hard-block domain list starts empty. Add any other time sinks.
- Extension ID format: pick a handle for `heimdall@<handle>`.

---

## Sources
- Firefox `ExtensionSettings` policy (force_installed, private_browsing): https://firefox-admin-docs.mozilla.org/reference/policies/extensionsettings/
- Firefox 153 enterprise release notes (MV3 host permissions locked): https://support.mozilla.org/bs/kb/firefox-enterprise-153-release-notes
- Firefox snap policies path: https://discourse.ubuntu.com/t/how-to-customize-the-firefox-snap-with-enterprise-policies/23074
- Snap local XPI location: https://discourse.mozilla.org/t/ubuntu-firefox-snap-local-extension-addon-registration/139104
- Firefox policy templates (DisableSafeMode, etc.): https://mozilla.github.io/policy-templates/
- web-ext unlisted signing: https://github.com/mozilla/web-ext/issues/2669
- Cloudflare Clef announcement: https://blog.cloudflare.com/clef-decision-models
- Clef on Workers AI (changelog): https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/
- Clef-flash price cut / context change: https://latenode.com/news/cloudflare-cuts-clef-flash-price-trims-hosted-context-to-24k
- Workers AI pricing & free allocation: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Clef-flash local GGUF / VRAM: https://iii.dev/registry/workers/judge-clef.md
- Independent decision-API comparison (700 items): https://pub.towardsai.net/jev-vs-clef-vs-openai-decisions-api-i-tested-six-decision-apis-on-700-items-05404e886126
- Jev pricing and limits: https://flaviocopes.com/jev-pricing/
- System One models overview (calibration caveat): https://www.dsebastien.net/system-one-models/
- Laya vs Decider (Laya zero-shot caveat): https://www.orcarouter.ai/blog/laya-vs-decider
- Claude Haiku 5.5 pricing: https://techenclave.com/news/items/claude-haiku-5-5-launches-with-0-10-input-pricing-for-shorter-prompts
