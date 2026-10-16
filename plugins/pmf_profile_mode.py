#
# PMF.RV Profile Mode
#
# Asks each person for their name and employee ID once (mandatory, first
# launch) and uses it as their Sync Review network name, e.g.
# "Alex_EMP1023". Without this every studio PC appears as the shared
# Windows login ("Administrator") and people can only be told apart by
# machine name or by renaming contacts by hand.
#
# Stored per Windows user in %APPDATA%\PMF.RV\profile.json -- outside the
# PMF.RVTOOL folder, so auto-updates never touch it. Editable later via
# Advanced -> My Profile...
#
# This is self-declared identification, not authentication: nothing
# verifies the name/ID (there is no studio directory to check against).
#

import json
import os
import re

from rv import rvtypes, commands, extra_commands

NAME_MAX = 24
EMP_MAX = 32  # employee IDs have no fixed format or prefix


def _profile_path():
    base = os.environ.get("PMF_RV_PROFILE_DIR") or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "PMF.RV")
    return os.path.join(base, "profile.json")


def _clean(s, limit):
    # RV's sync protocol carries this name inside "name@host" and
    # "contents|session|origin" strings: spaces, '@', '|', '&' etc. break
    # parsing. Letters, digits, '_' and '-' only.
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", (s or "").strip())
    return re.sub(r"_+", "_", s).strip("_")[:limit]


def load_profile():
    try:
        # utf-8-sig: profiles written by PowerShell 5.1 (e.g. pre-created by
        # IT) start with a BOM, which plain utf-8 json.load rejects.
        with open(_profile_path(), "r", encoding="utf-8-sig") as f:
            p = json.load(f)
        if p.get("name", "").strip() and p.get("emp_id", "").strip():
            return p
    except Exception:
        pass
    return None


def save_profile(name, emp_id):
    path = _profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": name.strip(), "emp_id": emp_id.strip()}, f, indent=2)


def contact_name_for(name, emp_id):
    """What other people see, e.g. 'Alex_EMP1023'. The employee ID keeps
    it unique even when two people share a first name."""
    n, e = _clean(name, NAME_MAX), _clean(emp_id, EMP_MAX)
    return "%s_%s" % (n, e) if n and e else (n or e)


def profile_contact_name():
    """Used by the Room and Notify plugins. None until a profile exists."""
    p = load_profile()
    return contact_name_for(p["name"], p["emp_id"]) if p else None


def profile_display_name():
    p = load_profile()
    return "%s (%s)" % (p["name"].strip(), p["emp_id"].strip()) if p else None


def _clear_participant_list():
    # Same reason as in pmf_room_mode: a non-empty list makes RV's network
    # stop show a modal "connections still active" box.
    from PySide6.QtWidgets import QApplication, QListWidget, QPushButton
    for w in QApplication.allWidgets():
        if (isinstance(w, QListWidget) and w.objectName() == "participantList"
                and w.window().findChild(QPushButton, "startButton") is not None):
            w.clear()


def _set_name_field(name):
    # NOT commands.setRemoteLocalContactName: in current OpenRV builds it is
    # registered as returning string but implemented as void and crashes
    # rv.exe when called from Python. Its C++ body is just this setText.
    from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton
    for w in QApplication.allWidgets():
        if (isinstance(w, QLineEdit) and w.objectName() == "nameLineEdit"
                and w.window().findChild(QPushButton, "startButton") is not None):
            w.setText(name)
            return True
    return False


def apply_contact_name(allow_restart=True):
    """Make RV's network name match the profile. RV reads the name when its
    network starts, so a running network is restarted -- but never while
    someone is connected (that would drop them). Returns 'applied',
    'unchanged', 'pending' (applies on next network start) or 'noprofile'."""
    desired = profile_contact_name()
    if not desired:
        return "noprofile"
    if commands.remoteLocalContactName() == desired:
        return "unchanged"

    running = commands.remoteNetworkStatus() == 1
    if running and (not allow_restart or list(commands.remoteConnections())):
        # Leave the field alone: it must keep matching the name the live
        # network is actually using, so the next Create/Join Room (or next
        # launch) sees the mismatch and applies the profile name then.
        return "pending"

    if running:
        _clear_participant_list()
        commands.remoteNetwork(False)
    _set_name_field(desired)
    if running:
        commands.remoteNetwork(True)
    return "applied"


class _ProfileDialog(object):
    """Built lazily so importing this module never needs a QApplication."""

    @staticmethod
    def run(parent, mandatory, current=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLabel,
                                       QLineEdit, QDialogButtonBox)

        class Dlg(QDialog):
            def reject(self):
                if not mandatory:
                    QDialog.reject(self)
                # mandatory: Esc and the close button do nothing

        dlg = Dlg(parent)
        dlg.setWindowTitle("PMF.RV -- Who are you?" if mandatory else "PMF.RV -- My Profile")
        if mandatory:
            dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowCloseButtonHint)

        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            "Enter your name and employee ID. This is how other people will see\n"
            "you in review sessions (contacts, pointer labels, rooms)."))

        form = QFormLayout()
        name_edit = QLineEdit((current or {}).get("name", ""))
        emp_edit = QLineEdit((current or {}).get("emp_id", ""))
        name_edit.setPlaceholderText("e.g. Alex")
        emp_edit.setPlaceholderText("your employee ID")
        form.addRow("Your name:", name_edit)
        form.addRow("Employee ID:", emp_edit)
        layout.addLayout(form)

        preview = QLabel()
        layout.addWidget(preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok if mandatory
                                   else QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        ok_btn = buttons.button(QDialogButtonBox.Ok)

        def refresh():
            n, e = name_edit.text(), emp_edit.text()
            valid = bool(_clean(n, NAME_MAX)) and bool(_clean(e, EMP_MAX))
            ok_btn.setEnabled(valid)
            preview.setText("Others will see you as:  <b>%s</b>" % contact_name_for(n, e)
                            if valid else "<i>Both fields are required.</i>")

        name_edit.textChanged.connect(refresh)
        emp_edit.textChanged.connect(refresh)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        refresh()

        if dlg.exec_() == QDialog.Accepted:
            return name_edit.text().strip(), emp_edit.text().strip()
        return None


class PMFProfileMode(rvtypes.MinorMode):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self.init(
            "pmf-profile-mode",
            [],
            None,
            [("Advanced", [
                ("My Profile...", self._edit_profile, None, None),
            ])],
            "zz_pmf_profile",
            0,
        )
        try:
            from PySide6.QtCore import QTimer
            # After RV's window and network are up.
            QTimer.singleShot(1500, self._startup)
        except Exception as e:
            print("PMF.RV Profile: startup check unavailable (%s)" % e)

    def _window(self):
        import rv.qtutils
        return rv.qtutils.sessionWindow()

    def _startup(self):
        if load_profile() is None:
            result = _ProfileDialog.run(self._window(), mandatory=True)
            save_profile(*result)
        status = apply_contact_name(allow_restart=True)
        print("PMF.RV Profile: network name %s -> %s" % (status, commands.remoteLocalContactName()))

    def _edit_profile(self, event=None):
        result = _ProfileDialog.run(self._window(), mandatory=False, current=load_profile())
        if not result:
            return
        save_profile(*result)
        status = apply_contact_name(allow_restart=True)
        name = profile_contact_name()
        if status == "pending":
            extra_commands.displayFeedback(
                "Profile saved -- others will see you as %s after your current session ends." % name, 6.0)
        else:
            extra_commands.displayFeedback("Others now see you as %s." % name, 5.0)


def createMode():
    return PMFProfileMode()
