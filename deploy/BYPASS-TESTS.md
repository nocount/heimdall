# Bypass tests

Run these during focus hours after `deploy/install.sh` and a Firefox restart.
Each should fail to get you to a blocked page. For testing outside 9–5, set
`force_focus: true` in /etc/heimdall/config.yaml (`sudoedit`, then
`sudo systemctl restart heimdall heimdall-watchdog`, and pass
`--allow-force-focus` to install.sh if you re-run it). Set it back to `null`
afterwards.

Watch the watchdog while testing: `journalctl -u heimdall-watchdog -f`

| # | Attempt | Expected |
|---|---------|----------|
| 1 | Open a non-allowlisted YouTube video in a private window | Block page (the policy runs heimdall in private windows) |
| 2 | `firefox -P`, create a new profile, open the video | Block page: the policy applies to every profile |
| 3 | about:addons: disable or remove heimdall | No Disable/Remove controls; the extension stays |
| 4 | about:profiles | Blocked by policy |
| 5 | Help → Troubleshoot Mode (safe mode) | Menu entry missing (DisableSafeMode) |
| 6 | Hold Shift while launching Firefox (safe mode) | Extensions off, so no heartbeat: the watchdog closes Firefox within ~2.5 min of the last heartbeat (150s timeout, after a 60s startup grace) |
| 7 | `systemctl stop heimdall` without sudo | Asks for an admin password (polkit). If stopped with sudo, pages fail closed: "heimdall can't reach its daemon" |
| 8 | `kill` the daemon process as yourself | Permission denied (it runs as user heimdall) |
| 9 | `flatpak install --user flathub org.chromium.Chromium`, launch it | The watchdog kills it within ~15s |
| 10 | Snap/deb Chromium, Brave, Chrome, Vivaldi, Edge | Killed within ~15s |
| 11 | DevTools: delete `#heimdall-curtain` on a video page | Nothing gained: a block still redirects; an allowed page was going to be uncovered anyway |
| 12 | DevTools on the block page: edit it, or navigate the tab back | The redirect repeats on any return to the blocked URL; there's no client-side state to forge |
| 13 | about:debugging → load a temporary add-on with id heimdall@nocount | Should be refused while the policy version is installed; if not, note it |
| 14 | Visit `https://www.youtube-nocookie.com/embed/<id>` or an embed on another site | **Expected to leak** (PLAN.md section 12). Note what you find |
| 15 | Invidious / Piped front-ends | **Expected to leak**; add their domains to `hard_block_domains` as you meet them |
| 16 | Edit /etc/heimdall/config.yaml or /opt/heimdall without sudo | Permission denied |
| 17 | Copy the Firefox binary under another name and run it | Policy still applies (system-wide /etc/firefox/policies), so heimdall still loads. A renamed *Chromium* binary dodges the kill list: accepted (PLAN.md section 12) |

Record anything that gets through in PLAN.md section 12.
