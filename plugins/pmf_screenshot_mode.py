#
# PMF.RV Screenshot Mode
#
# Saves the current annotated viewer frame (canvas only, no UI chrome)
# to the user's Downloads folder as:
#   <VideoName>_frame<FrameNumber>.png
#
# Access:
#   - Small camera icon in the menu bar's top-right corner (same row as
#     File/Edit/.../Help) -- native C++ button added in RvDocument.cpp,
#     since RV's real QMenuBar isn't reliably reachable from a Python
#     plugin. Clicking it fires the "pmf-screenshot-request" generic
#     event, bound below, so the actual save logic stays in one place
#     (this file) instead of being duplicated in C++.
#   - Menu:   Advanced -> Save Frame Screenshot
#   - Hotkey: F9  (single key, no modifier chord — checked against every
#     key-down--f* binding already used by RV/plugins in this build; free)
#

import os
import re

from rv import rvtypes, commands, extra_commands


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _downloads_dir():
    """
    Resolve the user's Downloads folder.
    Windows:  C:\\Users\\<name>\\Downloads
    Fallback: home directory.
    """
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    if os.path.isdir(downloads):
        return downloads
    return home


def _safe_basename(path):
    """
    Extract filename stem from a media path, sanitized for filesystem use.
    e.g. '/mnt/proj/MyShot_v003.mov' -> 'MyShot_v003'
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem)
    return stem if stem else "clip"


def _unique_path(directory, base_name, ext=".png"):
    """
    Return a file path that doesn't already exist.
    If base_name.png exists, tries base_name_1.png, _2.png, etc.
    """
    path = os.path.join(directory, base_name + ext)
    if not os.path.exists(path):
        return path
    counter = 1
    while True:
        path = os.path.join(directory, "%s_%d%s" % (base_name, counter, ext))
        if not os.path.exists(path):
            return path
        counter += 1


# -----------------------------------------------------------------------
# Mode
# -----------------------------------------------------------------------

class PMFScreenshotMode(rvtypes.MinorMode):
    """
    Captures the viewer canvas (image + annotation overlays, no UI
    chrome) to a PNG in Downloads, named <VideoName>_frame<FrameNumber>.png.
    Reachable via the native menu-bar camera icon, the Advanced menu, or F9.
    """

    def __init__(self):
        rvtypes.MinorMode.__init__(self)

        self.init(
            "pmf-screenshot-mode",
            [
                # Hotkey: F9 — single key, nothing to hold down at the
                # same time. No modifier chord means no modifier-order/
                # format risk (that's what broke the original Ctrl+Alt+S
                # binding).
                ("key-down--f9", self._save_screenshot, "Save Frame Screenshot"),
                # Fired by the native camera-icon button in the menu bar
                # (RvDocument.cpp) via session()->userGenericEvent(...).
                ("pmf-screenshot-request", self._save_screenshot, "Save Frame Screenshot (from menu bar button)"),
            ],
            None,
            # Menu entry under PMF.RV as secondary access point
            [("Advanced", [
                ("Save Frame Screenshot  (F9)",
                 self._save_screenshot, None, None),
            ])],
            "zz_pmf_screenshot",
            0,
        )

    # ------------------------------------------------------------------
    # Core screenshot logic
    # ------------------------------------------------------------------

    def _save_screenshot(self, event=None):
        """
        Capture the current annotated frame to a PNG in the Downloads folder.
        Filename: <VideoName>_frame<FrameNumber>.png
        """
        try:
            # 1. Current frame number
            current_frame = commands.frame()

            # 2. Resolve the media filename (and extension) for this frame
            video_name = "clip"
            media_ext  = ""
            try:
                sources = commands.sourcesAtFrame(current_frame)
                if sources:
                    media_prop  = "%s.media.movie" % sources[0]
                    media_paths = commands.getStringProperty(media_prop)
                    if media_paths and media_paths[0]:
                        video_name = _safe_basename(media_paths[0])
                        media_ext  = os.path.splitext(media_paths[0])[1].lower()
            except Exception:
                video_name = "clip"

            # 2b. Frame-numbering convention differs by container: .mp4
            # counts its first frame as 1 (matches RV's frame() directly),
            # but .mov counts its first frame as 0 -- confirmed by
            # comparing RV's displayed frame number against the same
            # frame saved from each container type. Subtract 1 only for
            # containers confirmed to start at 0.
            ZERO_BASED_EXTENSIONS = (".mov",)
            if media_ext in ZERO_BASED_EXTENSIONS:
                save_frame = max(0, current_frame - 1)
            else:
                save_frame = current_frame

            # 3. Build filename:  <VideoName>_frame<padded_frame>.png
            base_name = "%s_frame%04d" % (video_name, save_frame)

            # 4. Unique path in Downloads
            out_dir  = _downloads_dir()
            out_path = _unique_path(out_dir, base_name, ".png")

            # 5. Save viewer canvas — image + annotation overlays, no UI chrome
            # (RV has no "saveCurrentFrame" command; the real one is
            # exportCurrentFrame, the same one RV's own File > Snapshot uses)
            commands.exportCurrentFrame(out_path)

            # 6. HUD toast
            filename = os.path.basename(out_path)
            extra_commands.displayFeedback(
                "Screenshot saved:  %s" % filename, 4.0
            )
            print("PMF.RV Screenshot: saved -> %s" % out_path)

        except Exception as e:
            msg = "Screenshot failed: %s" % str(e)
            print("PMF.RV Screenshot ERROR: %s" % msg)
            try:
                extra_commands.displayFeedback(msg, 5.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # RV mode lifecycle
    # ------------------------------------------------------------------

    def activate(self):
        rvtypes.MinorMode.activate(self)

    def deactivate(self):
        rvtypes.MinorMode.deactivate(self)


def createMode():
    """Required by RV's plugin system."""
    return PMFScreenshotMode()
