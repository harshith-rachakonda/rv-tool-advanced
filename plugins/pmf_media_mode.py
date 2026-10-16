#
# PMF.RV Review Media
#
# Lets someone OUTSIDE the studio network (the director on set, crew at
# home) review with everyone else.
#
# A Room / Sync Review session never sends the video itself -- only "open
# this file, go to frame 31, here's a stroke". Every PC opens the file from
# the same path, e.g. \\fileserver\Project\shot_010\comp_v003.mov, which an
# outsider can't reach. They are sent the review files beforehand instead
# and put them in their Review Media folder (default: Documents\PMF.RV Review
# Media; Advanced -> Review Media Folder...). When a file can't be found at its
# own path, this plugin looks for it there by name -- sub-folders and image
# sequences included -- and opens the local copy instead.
#
# Uses RV's "incoming-source-path" event, which RV raises for every file it
# loads (opened by hand, from a session file, or by Sync Review for a room).
# Paths that exist are never touched, so this does nothing in the office.
#
# Copies made by Teams / browser downloads get numbered endings -- "shot
# (1).mov", "shot (2).mov", "shot (1) (1).mov", "shot (1) (2).mov"... An exact
# name match always wins; failing that, those endings are ignored, so the two
# sides don't need identical names.
#

import json
import os
import re
import threading
import time

from rv import rvtypes, extra_commands

MAX_INDEX_FILES = 200000
UNREACHABLE_RETRY_S = 60
EXISTS_TIMEOUT_S = 2.0

# RV sequence notation in a file name: "shot.1001-1100#.exr", "shot.#.exr",
# "shot.@@@@.exr", "shot.%04d.exr", "shot.1-50@@@@.exr"
_SEQ_TOKEN = re.compile(r"^(?P<prefix>.*?)(?:-?\d+(?:-\d+)?(?:x\d+)?)?(?:#+|@+|%0?\d*d)(?P<suffix>[^#@%]*)$")
# a single frame on disk: "shot.1001.exr"
_FRAME_FILE = re.compile(r"^(?P<prefix>.*?)(?P<frame>-?\d+)(?P<suffix>\.[^.\\/]+)$")


_COPY_SUFFIX = re.compile(r"(\s*\(\d+\))+$")


def _loose_name(name):
    """'Shot (1) (2).MOV' -> 'shot.mov': numbered copy endings removed."""
    stem, ext = os.path.splitext(name)
    bare = _COPY_SUFFIX.sub("", stem).strip()
    return ((bare or stem) + ext).lower()


def _settings_path():
    base = os.environ.get("PMF_RV_PROFILE_DIR") or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "PMF.RV")
    return os.path.join(base, "settings.json")


def _load_settings():
    try:
        with open(_settings_path(), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_settings(data):
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def default_media_folder():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "PMF.RV Review Media")


def media_folder():
    return _load_settings().get("review_media_folder") or default_media_folder()


def set_media_folder(folder):
    data = _load_settings()
    data["review_media_folder"] = folder
    _save_settings(data)


def _root_of(path):
    """\\\\server\\share or a drive letter -- the part that can be unreachable."""
    p = path.replace("/", "\\")
    if p.startswith("\\\\"):
        parts = p[2:].split("\\")
        return "\\\\" + "\\".join(parts[:2]).lower()
    drive, _ = os.path.splitdrive(p)
    return drive.lower() or None


class _Locator(object):
    """Finds the local copy of a missing file. Never blocks RV for long: a
    path on an unreachable server (an outsider's \\\\fileserver) would make a
    plain os.path.exists wait ~20 s per file."""

    def __init__(self):
        self._unreachable = {}     # root -> time it last timed out
        self._index = None         # (folder, built_at, files, frames)
        self._lock = threading.Lock()

    def _exists(self, path):
        root = _root_of(path)
        if root and time.time() - self._unreachable.get(root, 0) < UNREACHABLE_RETRY_S:
            return False
        result = {}
        done = threading.Event()

        def check():
            try:
                result["ok"] = os.path.exists(path)
            except Exception:
                result["ok"] = False
            done.set()

        t = threading.Thread(target=check, name="pmf-media-exists")
        t.daemon = True
        t.start()
        if not done.wait(EXISTS_TIMEOUT_S):
            if root:
                self._unreachable[root] = time.time()
            return False
        return result.get("ok", False)

    def _build_index(self, folder):
        files, loose, frames, count = {}, {}, {}, 0
        for dirpath, _dirs, names in os.walk(folder):
            for n in names:
                count += 1
                if count > MAX_INDEX_FILES:
                    break
                files.setdefault(n.lower(), []).append(os.path.join(dirpath, n))
                loose.setdefault(_loose_name(n), []).append(os.path.join(dirpath, n))
                m = _FRAME_FILE.match(n)
                if m:
                    key = (m.group("prefix").lower(), m.group("suffix").lower())
                    frames.setdefault(key, set()).add(dirpath)
        return (folder, time.time(), files, loose, frames)

    def _get_index(self, folder, refresh=False):
        with self._lock:
            idx = self._index
            if refresh or idx is None or idx[0] != folder or time.time() - idx[1] > 30:
                idx = self._index = self._build_index(folder)
            return idx

    @staticmethod
    def _best(original, candidates):
        """Prefer the copy whose parent folders match the original's
        (two shots can both have comp_v003.mov)."""
        want = [p.lower() for p in re.split(r"[\\/]+", os.path.dirname(original)) if p][::-1]

        def score(c):
            have = [p.lower() for p in re.split(r"[\\/]+", os.path.dirname(c)) if p][::-1]
            n = 0
            for a, b in zip(want, have):
                if a != b:
                    break
                n += 1
            return n

        return max(sorted(candidates), key=score)

    def find(self, path):
        """Local copy of `path`, or None. `path` itself if it exists."""
        base = re.split(r"[\\/]", path)[-1]
        seq = _SEQ_TOKEN.match(base) if re.search(r"[#@]|%0?\d*d", base) else None

        if seq:
            if self._exists(os.path.dirname(path)):
                return path
        elif self._exists(path):
            return path

        folder = media_folder()
        if not os.path.isdir(folder):
            return None
        for refresh in (False, True):   # a file just copied in: rebuild once
            _f, _t, files, loose, frames = self._get_index(folder, refresh)
            if seq:
                dirs = frames.get((seq.group("prefix").lower(), seq.group("suffix").lower()))
                if dirs:
                    return os.path.join(self._best(path, dirs) if len(dirs) > 1 else next(iter(dirs)), base)
            else:
                hits = files.get(base.lower())
                if hits:
                    return self._best(path, hits)
        # No exact name, even after a fresh look: accept a numbered copy.
        if not seq:
            hits = loose.get(_loose_name(base))
            if hits:
                return self._best(path, hits)
        return None


class PMFMediaMode(rvtypes.MinorMode):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self._locator = _Locator()
        self._told = set()
        self.init(
            "pmf-media-mode",
            [("incoming-source-path", self._incoming, "PMF.RV Review Media: use local copies of unreachable files")],
            None,
            [("Advanced", [
                ("Review Media Folder...", self._folder_dialog, None, None),
            ])],
            "zz_pmf_media",
            0,
        )
        try:
            os.makedirs(media_folder(), exist_ok=True)
        except Exception:
            pass

    def _tell(self, key, msg):
        if key in self._told:
            return
        self._told.add(key)
        print("PMF.RV Review Media: " + msg)
        try:
            extra_commands.displayFeedback(msg, 6.0)
        except Exception:
            pass

    def _incoming(self, event):
        event.reject()   # let other handlers see it too
        try:
            previous = event.returnContents()
            parts = event.contents().split(";;")
            inpath = previous or parts[0]
            if not inpath or "://" in inpath or inpath.endswith(".movieproc") or inpath.endswith(".rv"):
                return
            local = self._locator.find(inpath)
            name = re.split(r"[\\/]", inpath)[-1]
            if local and local != inpath:
                event.setReturnContent(local)
                self._tell(("used", local), "Using your local copy of %s (Review Media folder)." % name)
            elif not local:
                self._tell(("missing", inpath),
                           "Can't find %s -- put a copy in your Review Media folder "
                           "(Advanced -> Review Media Folder...)." % name)
        except Exception as e:
            print("PMF.RV Review Media: %s" % e)

    def locate(self, path):
        """For tests through rvpush."""
        return self._locator.find(path)

    def _folder_dialog(self, event=None):
        try:
            import subprocess
            from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                           QPushButton, QFileDialog)
            import rv.qtutils
            dlg = QDialog(rv.qtutils.sessionWindow())
            dlg.setWindowTitle("Review Media Folder")
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel(
                "Working outside the studio network? Put the review files you were\n"
                "sent in this folder (sub-folders are fine). When a room opens a file\n"
                "your PC can't reach, PMF.RV uses the copy from here instead."))
            path_label = QLabel(media_folder())
            path_label.setStyleSheet("font-weight: bold")
            layout.addWidget(path_label)
            row = QHBoxLayout()
            open_btn, change_btn, close_btn = QPushButton("Open Folder"), QPushButton("Change..."), QPushButton("Close")
            for b in (open_btn, change_btn, close_btn):
                row.addWidget(b)
            layout.addLayout(row)

            def do_open():
                os.makedirs(media_folder(), exist_ok=True)
                subprocess.Popen(["explorer", os.path.normpath(media_folder())])

            def do_change():
                chosen = QFileDialog.getExistingDirectory(dlg, "Review Media Folder", media_folder())
                if chosen:
                    set_media_folder(os.path.normpath(chosen))
                    path_label.setText(media_folder())

            open_btn.clicked.connect(do_open)
            change_btn.clicked.connect(do_change)
            close_btn.clicked.connect(dlg.accept)
            dlg.exec_()
        except Exception as e:
            print("PMF.RV Review Media: dialog error: %s" % e)


_mode = None


def createMode():
    global _mode
    _mode = PMFMediaMode()
    return _mode
