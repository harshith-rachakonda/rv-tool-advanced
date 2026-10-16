# Changelog

Version numbers are the studio's internal release numbers.

## 10.06 -- faster, safer, visible updates
- Releases carry a file list (`release_manifest.txt`); the launcher copies only changed
  files instead of comparing 10,000+ files over the network (seconds instead of minutes over a VPN).
- "Updating" window with progress; only one update per install at a time.
- The new version is recorded only after every file is verified; interrupted updates resume.
- A file list that doesn't match the release is rejected, and an update never deletes a
  large part of an install (both guards came from a failure found in sandbox testing).

## 10.05
- License banner removed; tool menu renamed "Advanced"; plain version numbers.
- Automatic updates for reviewers outside the office (over Tailscale).

## 10.04
- Review Media matches Teams/download copies (`shot (1) (1).mov` = `shot (1).mov`).

## 10.03 -- remote reviewers
- Review Media folder: when a file can't be reached at its studio path, the local copy is
  opened instead (by name, sub-folders and image sequences included).
- Notify a Friend is an invite: clicking the notification opens RV and joins the sender's room.

## 10.02 -- presence and roster
- Notify colleagues by registered name, across networks, through the room server.
- Who's in the Room: every participant sees who is in the room and who has left.

## 10.01 -- reliable auto-update
- Fixed: updates could never replace the running launcher or tray app (Windows refuses
  to overwrite a running .exe but allows renaming it). Self-heal plugin for stuck installs.
- One shipped configuration file for server addresses; settings migration keyed on content.

## 10 -- rooms across networks
- Room server (TCP relay): everyone connects outbound, so rooms work across Wi-Fi/Ethernet,
  subnets, NAT and locked-down PCs. Automatic rejoin after drops.
- First-launch profile (name + employee ID) used as the network name.
- OpenRV fixes: contact-name crash, stale peers on rejoin, sync relay loop guard.
