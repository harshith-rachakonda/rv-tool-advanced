# OpenRV patches

Changes to [OpenRV](https://github.com/AcademySoftwareFoundation/OpenRV) itself, made while
building the review toolkit. They apply to upstream commit:

    25fd0efa67bb477fabd30ae7bd1b86988d200d14 2026-05-14 fix: Apply RV_DLLEXPORT macro to all output plugin exports

Apply from the OpenRV repository root (`git apply openrv-patches/<name>.patch`).

| Patch | What and why |
|---|---|
| `0001-sync-relay-loop-guard` | Sync Review: in a star topology (everyone connected to the host) the host forwards each guest's events to the others. Graph events are forwarded only when external-event broadcasting is on, and a short duplicate-payload guard stops a cycle from re-inserting the same stroke forever. |
| `0002-fix-setRemoteLocalContactName-crash` | `setRemoteLocalContactName` was registered with a `string` return type but implemented as `void`; the Python wrapper read garbage from the return register and crashed RV (access violation) on every call. |
| `0003-forget-disconnected-peers` | `Client::removeConnection` keyed removal on `peerAddress()`, which is already null after a disconnect, so dropped peers were never removed and every rejoin was refused as "already connected". Connections are now removed by identity. |
| `0004-safe-usernames-in-annotation-sync` | Annotation sync embeds the user name in property paths and in Mu code that is evaluated on other machines; a name with characters such as `.` or `&` silently corrupted that code. Names are now encoded to a safe character set. |
| `0005-menu-bar-screenshot-button` | One-click "save frame with annotations" camera button docked in the menu bar. |
| `0006-contact-nicknames` | Optional per-contact nickname column (Rename... in the Contacts list); display only, never used for identity or permissions. |
| `0007-windows-build-ffmpeg-prores-yaml-cpp` | Windows build fixes: enable ProRes/DNxHD decoders, a pkg-config wrapper so FFmpeg's configure finds dependencies under MSYS, and yaml-cpp linked as the DLL that OCIO ships. |
