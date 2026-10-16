# Tests

| Test | What it covers | Run |
|---|---|---|
| `test_updater.ps1` | Launcher auto-update in a throw-away sandbox: manifest (file-list) updates, replacing the running launcher and tray app, interrupted updates, one-update-at-a-time lock, damaged/mismatched file lists, robocopy fallback, missing-login message (31 checks) | `.\scripts\build.ps1` then `.\tests\test_updater.ps1` |
| `relay_protocol_test.py` | Room server protocol: HOST / CHECK / JOIN / DATA, error replies, tunnels | start `build\PMFRoomServer.exe --port 45160`, then `python tests\relay_protocol_test.py 127.0.0.1 45160` |
| `relay_security_test.py` | DATA connections bound to the host's IP, rate limiting of code guessing | same server, `python tests\relay_security_test.py <this PC's LAN IP> 45160` |
| `relay_names_test.py` | Room roster (who's in / who left) and Notify presence: sign-in by name, delivery to several PCs, case-insensitive names, Unicode, line-injection, flood limit (26 checks) | same server, `python tests\relay_names_test.py 45160` |
| `test_media_locator.py` | Review Media file matching: unreachable servers, same file name in two shots, image sequences in every RV notation, Teams-style `(1) (1)` copies (22 checks) | `python tests\test_media_locator.py` |

End-to-end behaviour (three real RV instances in one room: annotations, frame sync,
relay outage and automatic rejoin, invites) was tested by driving RV through `rvpush`;
those scripts need a full RV build and are not included here.
