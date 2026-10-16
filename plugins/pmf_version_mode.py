#
# PMF.RV Version Mode
#
# Shows the installed version in the Advanced menu ("Version: 10.05"),
# read from VERSION.txt -- the same line the auto-updater compares.
#
# Replaces the old license mode (removed in pmf.10.05), which put a license
# label in the viewer and title bar. Sessions saved while that mode was active
# still carry the label in defaultSequence.ui.name, so it is cleared from any
# session that is opened.
#
import os

from rv import rvtypes, commands

_DIST_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
_LABEL_PROP = "defaultSequence.ui.name"


def _read_version():
    try:
        with open(os.path.join(_DIST_ROOT, "VERSION.txt"), "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("Internal Version"):
                    v = line.split(":", 1)[1].strip()
                    return v.split("-pmf.", 1)[1] if "-pmf." in v else v
    except Exception:
        pass
    return "unknown"


_VERSION = _read_version()


def clear_old_license_label():
    try:
        if commands.propertyExists(_LABEL_PROP):
            name = commands.getStringProperty(_LABEL_PROP)
            if name and ("Licensed to" in name[0] or "Unlicensed" in name[0]):
                commands.setStringProperty(_LABEL_PROP, ["Default Sequence"], True)
    except Exception as e:
        print("PMF.RV Version: could not reset the sequence label: %s" % e)


class PMFVersionMode(rvtypes.MinorMode):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self.init(
            "pmf-version-mode",
            [("after-session-read", self._after_session_read, "Clear old license label")],
            None,
            [("Advanced", [
                ("_", None, None, None),
                ("Version: %s" % _VERSION, None, None, None),
            ])],
            "zz_pmf_version",
            0,
        )

    def activate(self):
        rvtypes.MinorMode.activate(self)
        clear_old_license_label()

    def _after_session_read(self, event):
        event.reject()
        clear_old_license_label()


def createMode():
    return PMFVersionMode()
