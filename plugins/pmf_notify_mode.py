#
# PMF.RV Notify Mode
#
# Adds "Advanced -> Notify a Friend..." -- pops up a Windows notification on a
# colleague's PC even when their RV is closed, via their PMF.RV Notify tray
# companion (PMFNotify.exe, a separate always-on background app, NOT RV).
#
# Pick the person by NAME: every PC's Notify app signs in to the PMF.RV room
# server under its profile name (e.g. Alex_EMP0485), the dialog lists who
# is signed in right now, and the room server delivers the notification. It
# works on any network that reaches the room server -- nobody needs to know
# an IP address or computer name.
#
# It is an INVITE: the notification carries the sender's room (a room is
# created, with the sender as host, if they aren't in one). Clicking it opens
# PMF.RV on the other PC -- or brings it to the front -- and joins that room.
# The code rides at the end of the sender text, "Alex (EMP0485) [room
# amber-otter-4821]", so the room server needs no change and an older
# PMFNotify still shows a readable message.
#
# Typing a computer name or IP address still works too: if the name isn't
# signed in (older PMF.RV, or no room server reachable) it falls back to
# connecting straight to that PC's Notify app on TCP 45140 (same network only).
#

import getpass
import re
import socket

from rv import rvtypes, extra_commands

NOTIFY_PORT = 45140
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _room():
    import pmf_room_mode  # relay addresses + request helper live there
    return pmf_room_mode


def _who():
    """(relay address, sorted names signed in) from the first room server
    that answers; (None, []) when none does."""
    room = _room()
    for addr in room._relay_servers():
        try:
            r = room._relay_request(addr, "WHO", timeout=2.0)
        except Exception:
            continue
        if r and r.startswith("OK"):
            return addr, sorted((n for n in r[2:].strip().split(",") if n), key=str.lower)
    return None, []


def _send_direct(host, sender):
    s = socket.create_connection((host, NOTIFY_PORT), timeout=4)
    try:
        s.sendall(("PING:%s" % sender).encode("utf-8"))
    finally:
        s.close()


class PMFNotifyMode(rvtypes.MinorMode):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self.init(
            "pmf-notify-mode",
            [],
            None,
            [("Advanced", [
                ("Notify a Friend...", self._notify_friend, None, None),
            ])],
            "zz_pmf_notify",
            0,
        )

    def _local_name(self):
        try:
            import pmf_profile_mode
            name = pmf_profile_mode.profile_display_name()
            if name:
                return name
        except Exception:
            pass
        try:
            return getpass.getuser()
        except Exception:
            return "Someone"

    def _my_contact_name(self):
        try:
            import pmf_profile_mode
            return pmf_profile_mode.profile_contact_name()
        except Exception:
            return None

    def _window(self):
        import rv.qtutils
        return rv.qtutils.sessionWindow()

    def _message(self, text):
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self._window(), "Notify a Friend", text)
        except Exception:
            extra_commands.displayFeedback(text.replace("\n", " "), 8.0)

    def _ask_who(self, online, relay_ok):
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QComboBox,
                                       QDialogButtonBox)
        dlg = QDialog(self._window())
        dlg.setWindowTitle("Notify a Friend")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Who do you want to invite?\n"
                                "They get a Windows notification even if their PMF.RV is closed;\n"
                                "clicking it opens PMF.RV and puts them in your room\n"
                                "(a room is created for you if you aren't in one)."))
        box = QComboBox()
        box.setEditable(True)
        box.addItems(online)
        box.setCurrentIndex(-1)
        box.lineEdit().setPlaceholderText("Pick a name, or type a computer name / IP address")
        layout.addWidget(box)
        if relay_ok:
            info = ("%d %s online right now." % (len(online), "person is" if len(online) == 1 else "people are")
                    if online else "Nobody else is online right now.")
        else:
            info = ("The PMF.RV room server can't be reached, so names aren't available.\n"
                    "Type the computer name or IP address instead (same network only).")
        layout.addWidget(QLabel(info))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Notify")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return box.currentText().strip() or None

    def _ensure_room(self):
        """(code, note): the room the sender is in, else a newly created one."""
        room = _room()._mode
        if room._room_code:
            return room._room_code, None
        code, _note = room.create()
        return code, "Room '%s' created for you." % code

    def notify(self, target, relay_addr=None, invite=True):
        """Send one notification -- an invite to the sender's room unless
        invite=False. Returns (ok, message). Used by the dialog and by tests
        through rvpush."""
        sender = self._local_name()
        code = note = None
        if invite:
            try:
                code, note = self._ensure_room()
            except Exception as e:
                note = "Could not open a room (%s) -- sent a plain notification." % e
        if code:
            sender = "%s [room %s]" % (sender, code)
        ok, msg = self._send(target, sender, relay_addr)
        if ok and code:
            msg = "%s invited to room '%s' -- they join when they click the notification." % (target, code)
        if note:
            msg = note + " " + msg
        return ok, msg

    def _send(self, target, sender, relay_addr):
        if relay_addr is None:
            relay_addr, _ = _who()
        relay_answer = None
        if relay_addr and NAME_RE.match(target):
            try:
                relay_answer = _room()._relay_request(relay_addr, "NOTIFY %s %s" % (target, sender))
            except Exception as e:
                relay_answer = "unreachable (%s)" % e
            if relay_answer == "OK":
                return True, "Notification sent to %s." % target
            if relay_answer == "ERR ratelimited":
                return False, "Too many notifications in the last minute -- wait a moment and try again."
        # Not signed in by that name (older PMF.RV, or no room server): try it
        # as a computer name / IP address on the local network.
        try:
            _send_direct(target, sender)
            return True, "Notification sent to %s." % target
        except Exception as e:
            if relay_answer == "ERR offline":
                return False, ("%s isn't online right now -- their PMF.RV Notify isn't running, "
                               "or their PC is off." % target)
            return False, ("Could not notify %s.\n\nIf that's a person's name, they aren't online. "
                           "If it's a computer name or IP address, their PMF.RV Notify isn't "
                           "reachable (%s)." % (target, e))

    def _notify_friend(self, event=None):
        try:
            relay_addr, online = _who()
            me = (self._my_contact_name() or "").lower()
            online = [n for n in online if n.lower() != me]
            target = self._ask_who(online, relay_addr is not None)
            if not target:
                return
            ok, msg = self.notify(target, relay_addr)
            print("PMF.RV Notify: %s" % msg)
            if ok:
                extra_commands.displayFeedback(msg, 6.0)
            else:
                self._message(msg)
        except Exception as e:
            print("PMF.RV Notify ERROR: %s" % e)
            self._message("Could not send the notification (%s)" % e)


_mode = None


def createMode():
    global _mode
    _mode = PMFNotifyMode()
    return _mode
