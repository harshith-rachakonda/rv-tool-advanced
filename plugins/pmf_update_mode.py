#
# PMF.RV Update Self-Heal
#
# Finishes an auto-update that the launcher could not finish itself.
#
# RVTool.exe (pmf.9/pmf.10) mirrors the release over its own folder while
# it is running. Windows won't overwrite a running .exe, so any release
# that changed RVTool.exe or the always-on PMFNotify.exe failed on those
# two files every launch: everything else (including this plugin) was
# copied, but the launcher -- and with it the fix -- never was, and the
# version was never stamped.
#
# This runs inside RV, i.e. after that partial update. Windows does allow
# RENAMING a running exe, so for each root .exe that differs from the
# release it copies the new one in beside it, renames the running one to
# *.old and the new one into place. The next launch then uses the new
# RVTool.exe, which completes the update properly (and deletes *.old).
#
# Does nothing when the install is already at the release's version, on
# the dev machine (PMF.RVTOOL_disable_autoupdate.txt beside the folder), or
# when no update location is reachable. Runs in a background thread: an
# unreachable share must never slow RV down.
#

import configparser
import os
import shutil
import socket
import subprocess
import threading
import time

from rv import rvtypes

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
_PARENT = os.path.dirname(_ROOT)

BUILTIN_SOURCES = [r"\\192.0.2.10\PMF.RVTOOL_release", r"\\REVIEW-SERVER\PMF.RVTOOL_release"]
LEGACY_AUTO_SOURCE = r"\\192.0.2.10\PMF.RVTOOL_release"
EXES = ("RVTool.exe", "PMFNotify.exe", "SetupFirewall.exe")


def _log(msg):
    try:
        with open(os.path.join(_ROOT, "update_log.txt"), "a", encoding="utf-8") as f:
            f.write("%s  [self-heal] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _version(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Internal Version" in line and ":" in line:
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _sources():
    # Same order RVTool.exe uses: a deliberate manual override, then the
    # shipped config, then built-in.
    override = os.path.join(_PARENT, "PMF.RVTOOL_update_source.txt")
    try:
        with open(override, "r", encoding="utf-8-sig") as f:
            manual = f.read().strip()
        if manual and manual.rstrip("\\").lower() != LEGACY_AUTO_SOURCE.lower():
            return [manual]
    except Exception:
        pass
    cp = configparser.ConfigParser()
    try:
        cp.read(os.path.join(_ROOT, "app", "etc", "pmf_studio.ini"), encoding="utf-8")
        listed = [s.strip() for s in cp.get("update", "sources", fallback="").split(",") if s.strip()]
        if listed:
            return listed
    except Exception:
        pass
    return BUILTIN_SOURCES


def _smb_reachable(path):
    if not path.startswith("\\\\"):
        return True
    host = path[2:].split("\\", 1)[0]
    try:
        socket.create_connection((host, 445), timeout=1.5).close()
        return True
    except OSError:
        return False


def _same_file(a, b):
    sa, sb = os.stat(a), os.stat(b)
    # robocopy preserves timestamps, so size + mtime is what it compares too.
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def _replace(name, source):
    mine = os.path.join(_ROOT, name)
    theirs = os.path.join(source, name)
    if not os.path.isfile(theirs) or (os.path.isfile(mine) and _same_file(mine, theirs)):
        return False
    staged = mine + ".new"
    aside = "%s.%d.old" % (mine, int(time.time() * 1000))
    shutil.copy2(theirs, staged)          # full copy first: never a half-written exe
    if os.path.exists(mine):
        os.rename(mine, aside)            # allowed even while it is running
    try:
        os.rename(staged, mine)
    except Exception:
        if os.path.exists(aside) and not os.path.exists(mine):
            os.rename(aside, mine)
        raise
    return True


def _restart_notify():
    # The running PMFNotify is the old code (from the renamed file). Its
    # auto-start entry points at PMFNotify.exe, which is now the new one.
    subprocess.run(["taskkill", "/F", "/IM", "PMFNotify.exe"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.Popen([os.path.join(_ROOT, "PMFNotify.exe")], cwd=_ROOT,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)


def heal():
    if os.path.exists(os.path.join(_PARENT, "PMF.RVTOOL_disable_autoupdate.txt")):
        return "disabled"
    for source in _sources():
        if not _smb_reachable(source):
            continue
        remote = _version(os.path.join(source, "VERSION.txt"))
        if remote is None:
            continue
        local = _version(os.path.join(_ROOT, "VERSION.txt"))
        if local == remote:
            return "current"
        replaced = []
        for name in EXES:
            try:
                if _replace(name, source):
                    replaced.append(name)
            except Exception as e:
                _log("could not replace %s: %s" % (name, e))
        if replaced:
            _log("%s -> %s: launcher could not update %s itself; replaced them from %s. "
                 "The next launch finishes the update." % (local, remote, ", ".join(replaced), source))
        if "PMFNotify.exe" in replaced:
            try:
                _restart_notify()
            except Exception as e:
                _log("could not restart PMF.RV Notify: %s" % e)
        return "healed" if replaced else "pending"
    return "unreachable"


class PMFUpdateMode(rvtypes.MinorMode):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self.init("pmf-update-mode", [], None, None, "zz_pmf_update", 0)

        def run():
            time.sleep(5)  # let RV finish starting first
            try:
                heal()
            except Exception as e:
                _log("failed: %s" % e)

        threading.Thread(target=run, name="pmf-update-self-heal", daemon=True).start()


def createMode():
    return PMFUpdateMode()
