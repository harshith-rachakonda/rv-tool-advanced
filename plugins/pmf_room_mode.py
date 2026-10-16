#
# PMF.RV Room Mode
#
# Create Room / Join Room / Leave Room / Room Status -- a group joins one
# review session with a short code instead of typing each other's IPs.
#
# Connection paths, tried in order when joining:
#   1. Room server relay (PMFRoomServer.exe, [room] in app/etc/pmf_studio.ini).
#      Host and joiners only make OUTBOUND TCP connections to the relay,
#      which pipes RV's own Sync Review bytes between them. Works across
#      subnets, Wi-Fi vs Ethernet, NAT, switch client isolation and
#      machines whose firewall blocks inbound -- anything that can reach
#      the room server.
#   2. LAN broadcast fallback (UDP 45141), for when no room server is
#      reachable. Same-subnet only, and the joiner must be able to reach
#      the host directly.
#
# Topology is a star: everyone connects to the host's RV, and sync.mu's
# _broadcastExternalEvents relay (on by default) forwards each person's
# annotations/pointer/frame events to everyone else. Full-mesh
# "SyncWithContactsContacts" is deliberately OFF (see OpenRV_migrate.ini):
# it needs every machine to reach every other machine directly by
# hostname, which is exactly what fails across networks.
#
# Who is in the room: the relay tracks every participant by profile name
# (sent with HOST/JOIN), so EVERY participant can see the full room and who
# has left -- Advanced -> Who's in the Room..., plus "X joined / X left"
# messages. RV's own Contacts list can't show this: each guest is only
# connected to the host, never to the other guests.
#
# Invites: "Notify a Friend" carries the sender's room code; clicking the
# notification makes PMF.RV Notify write join_request.json next to the
# profile (and open or bring up PMF.RV), and this plugin joins that room.
#
# Threading rule: background threads only touch sockets. Anything that
# talks to RV or Qt goes through self._ui, drained on the main thread by a
# QTimer.
#

import configparser
import getpass
import json
import os
import queue
import random
import re
import select
import socket
import threading
import time

from rv import rvtypes, commands, extra_commands

ROOM_UDP_PORT = 45141
RELAY_DEFAULT_PORT = 45150
MEMBERS_POLL_S = 4
QUERY_PREFIX = "PMFROOM_QUERY:"
REPLY_PREFIX = "PMFROOM_REPLY:"
PING_INTERVAL_S = 20
RECONNECT_WINDOW_S = 60
HOST_REREGISTER_EVERY_S = 2
NOROOM_GRACE_S = 20

ADJECTIVES = [
    "amber", "azure", "bold", "brisk", "calm", "coral", "eager", "fleet",
    "gold", "grey", "jade", "keen", "lively", "lucky", "misty", "noble",
    "olive", "quiet", "rapid", "sharp", "silver", "solar", "swift", "violet",
]
NOUNS = [
    "otter", "falcon", "maple", "harbor", "canyon", "comet", "cedar",
    "delta", "ember", "fox", "glacier", "heron", "island", "juniper",
    "lagoon", "meadow", "nova", "orbit", "pepper", "quartz", "river",
    "summit", "tiger", "willow",
]

RELAY_ERRORS = {
    "ERR noroom": "no room with that code is open",
    "ERR closed": "the host has closed this room",
    "ERR taken": "that code is already in use",
    "ERR ratelimited": "too many failed attempts from this machine -- wait 5 minutes",
    "ERR roomfull": "the room is full",
    "ERR serverfull": "the room server is full",
    "ERR hostgone": "the room's host just disconnected",
    "ERR hosttimeout": "the room's host did not respond",
    "ERR badcode": "that is not a valid room code",
}


# ---- helpers ------------------------------------------------------------

def _generate_code():
    # ~5.2 million combinations; the relay also rate-limits failed lookups.
    return "%s-%s-%d" % (random.choice(ADJECTIVES), random.choice(NOUNS),
                         random.randint(1000, 9999))


def _sanitize(s):
    # Contact names travel inside "name@host" and "contents|session|origin"
    # strings in RV's sync protocol; '&', '@', '|', spaces etc. break that
    # parsing (the USER_R&D username already broke annotation sync once).
    return re.sub(r"[^A-Za-z0-9_-]", "_", s or "").strip("_")


def _local_user():
    try:
        return getpass.getuser()
    except Exception:
        return "user"


def _local_ip():
    # Routing-table lookup: a UDP "connect" sends no packet, it just picks
    # the interface the OS would use. Avoids RV's myNetworkHost(), which is
    # a machine NAME and depends on NetBIOS/mDNS resolution.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None


def _local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ip = _local_ip()
    if ip:
        ips.add(ip)
    return [i for i in ips if not i.startswith("127.") and not i.startswith("169.254.")]


def _set_contact_name(name):
    """Set RV's network contact name WITHOUT commands.setRemoteLocalContactName:
    in current OpenRV builds that command is registered as returning "string"
    but implemented as void (MuUICommands.cpp), so calling it from Python
    reads garbage as a string and crashes rv.exe (access violation 0xc0000005,
    reproduced 4/4). Its C++ body is just nameLineEdit->setText(name) on the
    network dialog, so do exactly that through Qt. Takes effect the next time
    RV's network starts; returns True when RV reports the new name."""
    from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton
    for w in QApplication.allWidgets():
        if (isinstance(w, QLineEdit) and w.objectName() == "nameLineEdit"
                and w.window().findChild(QPushButton, "startButton") is not None):
            w.setText(name)
            return commands.remoteLocalContactName() == name
    return False


def _clear_participant_list():
    """Empty the network dialog's participant list before we restart RV's
    network. toggleServer() shows a modal "N connections still active --
    Stop Network?" box when that list is non-empty, and stock RV leaves
    dropped peers in it, so the box appeared during automatic reconnects.
    While it is open, RV keeps processing network events inside the stop
    path, and both test guests crashed in that state (0xc0000005,
    rv.exe+0x33455c). The list is display-only and toggleServer clears it
    itself right after the prompt."""
    from PySide6.QtWidgets import QApplication, QListWidget, QPushButton
    for w in QApplication.allWidgets():
        if (isinstance(w, QListWidget) and w.objectName() == "participantList"
                and w.window().findChild(QPushButton, "startButton") is not None):
            w.clear()


def _rv_home():
    h = os.environ.get("RV_HOME")
    if h and os.path.isdir(h):
        return h
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _relay_servers():
    path = os.path.join(_rv_home(), "etc", "pmf_studio.ini")
    servers = []
    try:
        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        for item in cp.get("room", "servers", fallback="").split(","):
            item = item.strip()
            if not item:
                continue
            host, sep, port = item.rpartition(":")
            if not sep:
                host, port = item, str(RELAY_DEFAULT_PORT)
            servers.append((host.strip(), int(port)))
    except Exception as e:
        print("PMF.RV Room: could not read %s (%s)" % (path, e))
    return servers


def _send_line(sock, line):
    sock.sendall((line + "\n").encode("utf-8"))


def _read_line(sock, limit=256):
    # Byte at a time: anything after '\n' is RV stream data, not ours.
    buf = bytearray()
    while True:
        c = sock.recv(1)
        if not c:
            return None
        if c == b"\n":
            return buf.decode("utf-8", errors="replace").strip()
        buf += c
        if len(buf) > limit:
            return None


def _close(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


def _pipe(a, b, registry, on_close=None):
    registry.update([a, b])
    fired = [False]
    fired_lock = threading.Lock()

    def notify_once():
        with fired_lock:
            if fired[0]:
                return
            fired[0] = True
        on_close()

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            _close(src)
            _close(dst)
            registry.discard(src)
            registry.discard(dst)
            if on_close:
                notify_once()

    for src, dst in ((a, b), (b, a)):
        t = threading.Thread(target=pump, args=(src, dst))
        t.daemon = True
        t.start()


def _my_room_name():
    """Profile name (e.g. Alex_EMP0485) as the relay lists it -- without
    the per-join _rXXXX tag RV's own contact name carries for guests."""
    try:
        import pmf_profile_mode
        name = pmf_profile_mode.profile_contact_name()
        if name:
            return name
    except Exception:
        pass
    try:
        current = commands.remoteLocalContactName()
    except Exception:
        current = ""
    return re.sub(r"_r[0-9a-f]{4}$", "", _sanitize(current)) or "guest"


JOIN_REQUEST_MAX_AGE_S = 600


def _join_request_path():
    # Same folder as profile.json (pmf_profile_mode), where PMFNotify.exe
    # writes it when an invite notification is clicked.
    base = os.environ.get("PMF_RV_PROFILE_DIR") or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "PMF.RV")
    return os.path.join(base, "join_request.json")


def _parse_members(answer):
    """'OK host=A in=b,c left=d' -> {'host': 'A', 'in': [...], 'left': [...]}"""
    if not answer or not answer.startswith("OK "):
        return None
    out = {"host": "host", "in": [], "left": []}
    for part in answer[3:].split(" "):
        key, _, val = part.partition("=")
        if key == "host":
            out["host"] = val
        elif key in ("in", "left"):
            out[key] = [n for n in val.split(",") if n]
    return out


def _relay_request(addr, line, timeout=3.0):
    s = socket.create_connection(addr, timeout=timeout)
    try:
        s.settimeout(timeout)
        _send_line(s, line)
        return _read_line(s)
    finally:
        _close(s)


class RelayCodeTaken(Exception):
    pass


# ---- relay host session -------------------------------------------------

class RelayHost(object):
    """Holds the room open on the relay and answers OPEN requests by
    bridging a fresh relay DATA connection to this machine's own RV."""

    def __init__(self, addr, code, rv_target, post, name=None):
        self.addr = addr
        self.code = code
        self.name = name
        self.rv_target = rv_target
        self.post = post
        self.control = None
        self.write_lock = threading.Lock()
        self.tunnels = set()
        self.stopped = False

    def _register(self):
        s = socket.create_connection(self.addr, timeout=3)
        s.settimeout(5)
        _send_line(s, "HOST %s %s" % (self.code, self.name) if self.name else "HOST " + self.code)
        r = _read_line(s)
        if r == "ERR badcode" and self.name:
            # Room server older than pmf.10.02: no names in HOST.
            _close(s)
            self.name = None
            return self._register()
        if r == "ERR taken":
            _close(s)
            raise RelayCodeTaken()
        if r != "OK":
            _close(s)
            raise IOError(RELAY_ERRORS.get(r, r or "no answer"))
        s.settimeout(None)
        self.control = s

    def start(self):
        self._register()
        for target in (self._reader, self._pinger):
            t = threading.Thread(target=target)
            t.daemon = True
            t.start()

    def _pinger(self):
        while not self.stopped:
            time.sleep(PING_INTERVAL_S)
            if self.stopped:
                return
            try:
                with self.write_lock:
                    _send_line(self.control, "PING")
            except Exception:
                return  # reader notices the dead socket too

    def _reader(self):
        while not self.stopped:
            try:
                line = _read_line(self.control)
            except Exception:
                line = None
            if line is None:
                if not self.stopped:
                    self._on_lost()
                return
            if line.startswith("OPEN "):
                sid = line.split(" ", 1)[1]
                t = threading.Thread(target=self._open_tunnel, args=(sid,))
                t.daemon = True
                t.start()

    def _open_tunnel(self, sid):
        data = local = None
        try:
            data = socket.create_connection(self.addr, timeout=5)
            _send_line(data, "DATA " + sid)
            # Connect to our own RV via the LAN IP, not 127.0.0.1: RV treats
            # loopback peers as trusted and skips its connect-permission
            # policy (RvNetworkDialog.cpp, c->isLocal()).
            local = socket.create_connection(self.rv_target, timeout=5)
            data.settimeout(None)
            local.settimeout(None)
            _pipe(data, local, self.tunnels)
        except Exception as e:
            _close(data)
            _close(local)
            self.post("Room: could not connect a participant (%s)" % e)

    def _on_lost(self):
        _close(self.control)
        for s in list(self.tunnels):
            _close(s)
        self.post("Room '%s': lost the room server -- reconnecting..." % self.code)
        deadline = time.time() + RECONNECT_WINDOW_S
        while not self.stopped and time.time() < deadline:
            time.sleep(HOST_REREGISTER_EVERY_S)
            try:
                self._register()
            except RelayCodeTaken:
                break
            except Exception:
                continue
            self.post("Room '%s' is back online -- participants can Join again with the same code." % self.code)
            t = threading.Thread(target=self._reader)
            t.daemon = True
            t.start()
            return
        if not self.stopped:
            self.post("Room '%s' is no longer reachable through the room server. Use Leave Room, then Create Room again." % self.code)

    def stop(self):
        self.stopped = True
        try:
            with self.write_lock:
                _send_line(self.control, "BYE")
        except Exception:
            pass
        _close(self.control)
        for s in list(self.tunnels):
            _close(s)


# ---- LAN fallback -------------------------------------------------------

class LanResponder(object):
    def __init__(self, code, rv_port, name):
        self.code = code
        self.rv_port = rv_port
        self.name = name
        self.sock = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", ROOM_UDP_PORT))  # no SO_REUSEADDR: two binders would split delivery
        self.sock = s
        t = threading.Thread(target=self._loop)
        t.daemon = True
        t.start()

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(512)
            except Exception:
                return
            msg = data.decode("utf-8", errors="ignore").strip()
            if msg == QUERY_PREFIX + self.code:
                # Payload IP kept only for pmf.8/9 clients; current clients
                # use the reply's source address instead.
                reply = "%s%s:%s:%s:%s" % (REPLY_PREFIX, self.code, _local_ip() or "",
                                           self.rv_port, self.name)
                try:
                    self.sock.sendto(reply.encode("utf-8"), addr)
                except Exception:
                    pass

    def stop(self):
        _close(self.sock)


def _lan_find_room(code, timeout=2.5):
    # One socket per local interface: a 255.255.255.255 broadcast from an
    # unbound socket only leaves through one adapter on multi-homed PCs.
    socks = []
    for ip in _local_ipv4s():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            socks.append(s)
        except Exception:
            pass
    if not socks:
        return None

    query = (QUERY_PREFIX + code).encode("utf-8")
    deadline = time.time() + timeout
    next_send = 0
    try:
        while time.time() < deadline:
            if time.time() >= next_send:
                for s in socks:
                    try:
                        s.sendto(query, ("255.255.255.255", ROOM_UDP_PORT))
                    except Exception:
                        pass
                next_send = time.time() + 0.8
            ready, _, _ = select.select(socks, [], [], 0.2)
            for s in ready:
                try:
                    data, addr = s.recvfrom(512)
                except Exception:
                    continue
                msg = data.decode("utf-8", errors="ignore").strip()
                if not msg.startswith(REPLY_PREFIX):
                    continue
                parts = msg[len(REPLY_PREFIX):].split(":")
                if len(parts) >= 4 and parts[0] == code:
                    return (addr[0], int(parts[2]), parts[3])
    finally:
        for s in socks:
            _close(s)
    return None


# ---- mode ---------------------------------------------------------------

class PMFRoomMode(rvtypes.MinorMode):
    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self._room_code = None
        self._role = None          # "host" / "guest"
        self._via = None           # "relay host:port" / "LAN"
        self._relay_host = None
        self._lan = None
        self._guest_sockets = set()
        self._rejoin_code = None
        self._rejoin_deadline = 0
        self._rejoin_noroom_deadline = 0
        self._relay_answers = []
        self._members = None        # last roster from the relay
        self._watch_gen = 0         # bumping it stops the roster watcher
        self.recent_messages = []   # last on-screen messages (troubleshooting, tests)
        self.last_result = None
        self._ui = queue.Queue()
        self._timer = None

        self.init(
            "pmf-room-mode",
            [],
            None,
            [("Advanced", [
                ("Create Room...", self._create_room, None, None),
                ("Join Room...", self._join_room, None, None),
                ("Leave Room", self._leave_room, None, None),
                ("Who's in the Room...", self._room_status, None, None),
            ])],
            "zz_pmf_room",
            0,
        )

        try:
            from PySide6.QtCore import QTimer
            self._timer = QTimer()
            self._timer.timeout.connect(self._drain_ui)
            self._timer.start(500)
        except Exception as e:
            print("PMF.RV Room: UI message timer unavailable (%s)" % e)

    # -- main-thread plumbing --

    def _post(self, msg):
        self._ui.put(msg)

    def _drain_ui(self):
        self._check_join_request()
        while True:
            try:
                item = self._ui.get_nowait()
            except queue.Empty:
                return
            if callable(item):
                try:
                    item()
                except Exception as e:
                    print("PMF.RV Room: main-thread action failed (%s)" % e)
                continue
            print("PMF.RV Room: " + item)
            self.recent_messages = (self.recent_messages + [item])[-50:]
            try:
                extra_commands.displayFeedback(item, 6.0)
            except Exception:
                pass

    def _check_join_request(self):
        """Join the room from an invite the person just clicked."""
        path = _join_request_path()
        if not os.path.exists(path):
            return
        try:
            import pmf_profile_mode
            if pmf_profile_mode.load_profile() is None:
                return  # first launch: let them enter name + employee ID first
        except Exception:
            pass
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                req = json.load(f)
        except Exception:
            req = None
        try:
            os.remove(path)
        except Exception:
            return  # can't consume it: don't act on it every tick
        if not isinstance(req, dict):
            return
        code = str(req.get("code", "")).strip().lower()
        sender = str(req.get("from") or "Someone")
        try:
            age = time.time() - float(req.get("time", 0))
        except Exception:
            age = JOIN_REQUEST_MAX_AGE_S + 1
        if not re.match(r"^[a-z0-9-]{4,64}$", code) or age > JOIN_REQUEST_MAX_AGE_S:
            return
        if self._room_code == code:
            self._feedback("You are already in %s's room '%s'." % (sender, code), 4.0)
            return
        if self._room_code:
            try:
                from PySide6.QtWidgets import QMessageBox
                answer = QMessageBox.question(
                    self._window(), "Join Room",
                    "%s invited you to room '%s'.\n\nLeave your current room '%s' and join?"
                    % (sender, code, self._room_code))
                if answer != QMessageBox.Yes:
                    return
            except Exception:
                return
            self._leave_room()
        self._rejoin_code = None
        self._feedback("Joining %s's room '%s'..." % (sender, code), 4.0)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1000, lambda: self.join_code(code))

    def _feedback(self, msg, secs=6.0):
        print("PMF.RV Room: " + msg)
        self.recent_messages = (self.recent_messages + [msg])[-50:]
        try:
            extra_commands.displayFeedback(msg, secs)
        except Exception:
            pass

    def _window(self):
        import rv.qtutils
        return rv.qtutils.sessionWindow()

    def _message(self, title, text):
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self._window(), title, text)
        except Exception:
            self._feedback(text.replace("\n", " "), 10.0)

    # -- shared setup --

    def _prepare_network(self, guest):
        """Fresh RV network session with a collision-free contact name.
        Returns RV's listening port.

        Through the relay every peer reaches the host from the same address,
        so RV tells participants apart only by contact name: it must be
        unique per machine (two PCs logged in as "Admin" would reject each
        other as duplicates). Guests also add a per-join session tag
        (_rXXXX): stock RV never forgets a peer that dropped
        (TwkQtChat Client::removeConnection keyed its removal on
        peerAddress(), which is Null after disconnect), so a host that saw
        "guest" drop rejects that same name as "already connected" forever.
        A new tag per join sidesteps that; restarting the network clears
        this machine's own stale entries. Both are harmless once the
        Client.cpp fix ships in rv.exe.
        """
        commands.remoteNetwork(True)
        current = commands.remoteLocalContactName()
        try:
            import pmf_profile_mode  # name + employee ID, e.g. Alex_EMP1023
            base = pmf_profile_mode.profile_contact_name()
        except Exception:
            base = None
        if not base:
            # No profile yet: fall back to the login name plus machine name,
            # since many studio PCs share one Windows login.
            machine = _sanitize(socket.gethostname()) or "pc"
            base = re.sub(r"_r[0-9a-f]{4}$", "", _sanitize(current)) or _sanitize(_local_user()) or "user"
            if machine.lower() not in base.lower():
                base = "%s_%s" % (base, machine)
        desired = "%s_r%04x" % (base, random.randint(0, 0xFFFF)) if guest else base

        if desired != current:
            # Only restart when the name changes: a restart drops every live
            # Sync Review connection, so Create/Join start a fresh session.
            _clear_participant_list()
            commands.remoteNetwork(False)
            if not _set_contact_name(desired):
                print("PMF.RV Room: could not set contact name '%s'; keeping '%s'" % (desired, current))
            commands.remoteNetwork(True)  # RV reads the name when its server starts
        return commands.myNetworkPort()

    def _in_room_guard(self):
        if self._room_code:
            self._message("PMF.RV Room",
                          "You are already in room '%s'.\nUse Advanced -> Leave Room first "
                          "(joining two rooms would bridge them together)." % self._room_code)
            return True
        return False

    # -- Create Room --

    def _create_room(self, event=None):
        if self._in_room_guard():
            return
        try:
            code, note = self.create()
        except Exception as e:
            self._message("Create Room", str(e))
            return
        self._show_code_dialog(code, note)

    def create(self):
        """Open a room without UI prompts. Returns (code, note); raises on failure."""
        port = self._prepare_network(guest=False)
        if not port:
            raise IOError("RV networking did not start -- try again in a moment.")

        rv_target = (_local_ip() or "127.0.0.1", port)
        servers = _relay_servers()
        errors = []
        code = None

        for _attempt in range(5):
            code = _generate_code()
            taken = False
            for addr in servers:
                host = RelayHost(addr, code, rv_target, self._post, name=_my_room_name())
                try:
                    host.start()
                except RelayCodeTaken:
                    taken = True
                    break
                except Exception as e:
                    errors.append("%s:%s -- %s" % (addr[0], addr[1], e))
                    continue
                self._relay_host = host
                self._via = "room server %s:%s" % addr
                break
            if not taken:
                break

        try:
            self._lan = LanResponder(code, port, commands.remoteLocalContactName())
            self._lan.start()
        except Exception as e:
            self._lan = None
            print("PMF.RV Room: LAN fallback unavailable (%s)" % e)

        if not self._relay_host and not self._lan:
            raise IOError("Could not open a room.\n\nRoom server:\n  "
                          + ("\n  ".join(errors) or "none configured"))

        self._room_code = code
        self._role = "host"
        if not self._relay_host:
            self._via = "LAN only"
        else:
            self._start_member_watch(self._relay_host.addr, code)

        if self._relay_host:
            note = "Anyone who can reach the PMF.RV room server can join -- any network."
        else:
            note = ("The room server could not be reached, so this room is LAN-only:\n"
                    "joiners must be on your local network.\n\n" + "\n".join(errors))
        print("PMF.RV Room: hosting '%s' via %s" % (code, self._via))
        return code, note

    def _show_code_dialog(self, code, note):
        try:
            from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                           QLineEdit, QPushButton, QApplication)
            dlg = QDialog(self._window())
            dlg.setWindowTitle("Room Created")
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel("Share this room code with your team:"))
            field = QLineEdit(code)
            field.setReadOnly(True)
            field.selectAll()
            layout.addWidget(field)
            layout.addWidget(QLabel("They use Advanced -> Join Room... and enter it.\n" + note))
            row = QHBoxLayout()
            copy_btn = QPushButton("Copy Code")
            close_btn = QPushButton("Close")
            row.addWidget(copy_btn)
            row.addWidget(close_btn)
            layout.addLayout(row)

            def do_copy():
                QApplication.clipboard().setText(code)
                self._feedback("Room code copied to clipboard", 3.0)

            copy_btn.clicked.connect(do_copy)
            close_btn.clicked.connect(dlg.accept)
            dlg.exec_()
        except Exception as e:
            print("PMF.RV Room: could not show code dialog (%s)" % e)
            self._feedback("Room code: %s" % code, 15.0)

    # -- Join Room --

    def _join_room(self, event=None):
        if self._in_room_guard():
            return
        try:
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            code, ok = QInputDialog.getText(self._window(), "Join Room",
                                            "Room code (ask whoever created the room):",
                                            QLineEdit.Normal, "")
        except Exception as e:
            print("PMF.RV Room ERROR: could not show join dialog (%s)" % e)
            return
        if not ok:
            return
        code = code.strip().lower()
        if not code:
            return
        self.join_code(code)

    def join_code(self, code, interactive=True, allow_lan=True):
        """Join by code. interactive=False never opens a dialog (used by
        tests through rvpush and by auto-rejoin) and returns the failure
        text instead. self._relay_answers records each room server's reply
        so auto-rejoin can tell "room closed" from "server unreachable"."""
        self._relay_answers = []
        if not re.match(r"^[a-z0-9-]{4,64}$", code):
            msg = "'%s' is not a valid room code." % code
            if interactive:
                self._message("Join Room", msg)
            return msg
        try:
            self._prepare_network(guest=True)
        except Exception as e:
            if interactive:
                self._message("Join Room", "Could not start RV networking:\n%s" % e)
            return "error: %s" % e

        problems = []
        for addr in _relay_servers():
            try:
                r = _relay_request(addr, "CHECK " + code)
            except Exception as e:
                problems.append("Room server %s:%s unreachable (%s)" % (addr[0], addr[1], e))
                continue
            self._relay_answers.append(r)
            if r == "OK":
                self._join_via_relay(addr, code)
                return "relay"
            problems.append("Room server %s:%s: %s" % (addr[0], addr[1], RELAY_ERRORS.get(r, r)))
            if r in ("ERR ratelimited", "ERR badcode", "ERR closed"):
                break

        if allow_lan:
            self._feedback("Looking for room '%s' on the local network..." % code, 3.0)
            found = _lan_find_room(code)
            if found:
                ip, port, name = found
                commands.remoteConnect(name or ip, ip, port)
                self._room_code, self._role, self._via = code, "guest", "LAN %s" % ip
                self._feedback("Joining room '%s' on the local network (%s)..." % (code, ip))
                return "lan"
            problems.append("No host for '%s' answered on the local network" % code)

        msg = ("Could not join room '%s':\n\n- %s\n\nCheck the code, and that the "
               "host still has the room open." % (code, "\n- ".join(problems)))
        if interactive:
            self._message("Join Room", msg)
        return msg

    def _join_via_relay(self, addr, code):
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.bind(("127.0.0.1", 0))  # loopback only: no one else can ride this tunnel
        lst.listen(1)
        lst.settimeout(20)
        port = lst.getsockname()[1]
        sockets = self._guest_sockets
        post = self._post
        name = _my_room_name()

        def tunnel_closed():
            # Background thread: hand off to the main thread, which decides
            # whether this was a deliberate Leave (no-op) or a drop (rejoin).
            post(lambda: self._on_guest_tunnel_closed(code))

        def bridge():
            local = relay = None
            try:
                local, _ = lst.accept()
            except Exception as e:
                post("Room: RV did not open the room connection (%s)" % e)
                _close(lst)
                return
            _close(lst)
            try:
                relay = socket.create_connection(addr, timeout=5)
                relay.settimeout(20)
                _send_line(relay, "JOIN %s %s" % (code, name))
                r = _read_line(relay)
                if r == "ERR badcode":
                    # Room server older than pmf.10.02: no names in JOIN.
                    _close(relay)
                    relay = socket.create_connection(addr, timeout=5)
                    relay.settimeout(20)
                    _send_line(relay, "JOIN " + code)
                    r = _read_line(relay)
                if r != "OK":
                    raise IOError(RELAY_ERRORS.get(r, r or "no answer"))
                relay.settimeout(None)
                local.settimeout(None)
                _pipe(local, relay, sockets, on_close=tunnel_closed)
            except Exception as e:
                _close(local)
                _close(relay)
                post("Could not join room '%s': %s" % (code, e))

        t = threading.Thread(target=bridge)
        t.daemon = True
        t.start()
        commands.remoteConnect("room-" + code, "127.0.0.1", port)
        self._room_code, self._role, self._via = code, "guest", "room server %s:%s" % addr
        self._start_member_watch(addr, code)
        self._feedback("Joining room '%s' through the room server..." % code)

    # -- Leave / Status --

    def _on_guest_tunnel_closed(self, code):
        if self._room_code != code or self._role != "guest":
            return  # deliberate Leave Room, or already in another room
        self._feedback("Lost the connection to room '%s' -- reconnecting..." % code)
        self._stop_member_watch()
        self._room_code = self._role = self._via = None
        self._disconnect_all()  # clear the dead peer so RV doesn't prompt on restart
        self._rejoin_code = code
        self._rejoin_deadline = time.time() + RECONNECT_WINDOW_S
        self._rejoin_noroom_deadline = time.time() + NOROOM_GRACE_S
        self._schedule_rejoin(3000)

    def _schedule_rejoin(self, ms):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(ms, self._try_rejoin)

    def _try_rejoin(self):
        code = getattr(self, "_rejoin_code", None)
        if not code or self._room_code:
            return  # cancelled by Leave Room, or the user joined something else
        result = self.join_code(code, interactive=False, allow_lan=False)
        if result == "relay":
            self._rejoin_code = None
            self._feedback("Reconnected to room '%s'." % code)
            return
        if "ERR closed" in self._relay_answers:
            self._rejoin_code = None
            self._feedback("Room '%s' has been closed by its host." % code, 8.0)
            return
        if any(a in ("ERR badcode", "ERR ratelimited") for a in self._relay_answers):
            self._rejoin_code = None
            self._feedback("Could not reconnect to room '%s'." % code, 8.0)
            return
        # "noroom" right after an outage usually means the host has not
        # re-registered yet (the relay just restarted) -- only treat it as
        # "closed by the host" once it persists past a short grace period.
        if "ERR noroom" in self._relay_answers and time.time() > self._rejoin_noroom_deadline:
            self._rejoin_code = None
            self._feedback("Room '%s' has been closed by its host." % code, 8.0)
            return
        if time.time() < self._rejoin_deadline:
            self._schedule_rejoin(5000)  # room server unreachable -- keep trying
        else:
            self._rejoin_code = None
            self._feedback("Could not reconnect to room '%s' -- use Join Room to try again." % code, 10.0)

    def _leave_room(self, event=None):
        self._rejoin_code = None  # cancel any pending auto-rejoin
        if not self._room_code:
            self._feedback("You are not in a room.", 3.0)
            return
        code = self._room_code
        # Clear state BEFORE closing tunnels so their close callbacks see a
        # deliberate leave, not a drop to recover from.
        self._stop_member_watch()
        self._room_code = self._role = self._via = None
        if self._relay_host:
            self._relay_host.stop()
            self._relay_host = None
        if self._lan:
            self._lan.stop()
            self._lan = None
        for s in list(self._guest_sockets):
            _close(s)
        self._disconnect_all()
        self._feedback("Left room '%s'." % code, 4.0)

    def _disconnect_all(self):
        try:
            for c in commands.remoteConnections():
                commands.remoteDisconnect(c)
        except Exception:
            pass

    def later(self, action, *args, **kw):
        """Run create/join_code/_leave_room after delay_ms (default 1500)
        and keep the result in self.last_result. For requests that arrive
        over RV's own network (rvpush, remote eval): those actions may
        restart RV's networking, so wait until the requesting connection has
        closed -- otherwise it is still listed as an active participant and
        RV blocks on a "connections still active" dialog."""
        from PySide6.QtCore import QTimer
        self.last_result = "pending"

        def run():
            try:
                self.last_result = getattr(self, action)(*args)
            except Exception as e:
                self.last_result = "error: %s" % e

        QTimer.singleShot(kw.get("delay_ms", 1500), run)
        return "scheduled"

    # -- who is in the room --

    def _start_member_watch(self, addr, code):
        """Polls the relay's roster for this room; announces joins at once and
        leaves only after two polls (~8 s), so an auto-rejoin after a short
        drop doesn't flash "left" / "joined"."""
        self._watch_gen += 1
        gen = self._watch_gen
        self._members = None
        self._member_addr = addr
        me = _my_room_name()
        post = self._post

        def loop():
            known = None
            absent = {}
            while self._watch_gen == gen:
                try:
                    m = _parse_members(_relay_request(addr, "MEMBERS " + code))
                except Exception:
                    m = None
                if self._watch_gen != gen:
                    return
                if m:
                    self._members = m
                    present = set(m["in"]) | {m["host"]}
                    if known is None:
                        known = present
                    else:
                        for n in sorted(present - known):
                            if n != me:
                                post("%s joined the room." % n)
                        known |= present
                        for n in list(known - present):
                            absent[n] = absent.get(n, 0) + 1
                            if absent[n] >= 2:
                                known.discard(n)
                                absent.pop(n, None)
                                if n != me:
                                    post("%s left the room." % n)
                        for n in present:
                            absent.pop(n, None)
                time.sleep(MEMBERS_POLL_S)

        t = threading.Thread(target=loop, name="pmf-room-members")
        t.daemon = True
        t.start()

    def _stop_member_watch(self):
        self._watch_gen += 1
        self._members = None

    def members(self, fresh=False):
        """Roster dict (host/in/left) for a relayed room, else None."""
        if fresh and self._room_code and getattr(self, "_member_addr", None):
            try:
                m = _parse_members(_relay_request(self._member_addr, "MEMBERS " + self._room_code))
                if m:
                    self._members = m
            except Exception:
                pass
        return self._members

    def status(self):
        try:
            conns = list(commands.remoteConnections())
        except Exception:
            conns = []
        return {"code": self._room_code, "role": self._role, "via": self._via,
                "name": commands.remoteLocalContactName(), "connections": conns,
                "members": self._members}

    def _room_status(self, event=None):
        st = self.status()
        if not st["code"]:
            text = "You are not in a room.\n\nRoom servers configured:\n  %s" % (
                "\n  ".join("%s:%s" % a for a in _relay_servers()) or "none")
            self._message("Who's in the Room", text)
            return

        me = _my_room_name()
        lines = ["Room code:  %s" % st["code"],
                 "You are:  %s  (%s)" % (me, "the host" if st["role"] == "host" else "a guest"),
                 "Connected through:  %s" % st["via"], ""]
        m = self.members(fresh=True)
        if m:
            people = [m["host"]] + [n for n in m["in"] if n != m["host"]]
            lines.append("In the room now (%d):" % len(people))
            for n in people:
                tags = [t for t, on in (("host", n == m["host"]), ("you", n == me)) if on]
                lines.append("    %s%s" % (n, "   (%s)" % ", ".join(tags) if tags else ""))
            left = [n for n in m["left"] if n not in people]
            if left:
                lines += ["", "Left the room:"] + ["    %s" % n for n in left]
        else:
            # LAN-only room (no room server): only this PC's own connections are known.
            names = [re.sub(r"_r[0-9a-f]{4}$", "", c.split("@")[0]) for c in st["connections"]
                     if not c.startswith("rvpush")]
            lines.append("Connected to (this PC only -- LAN room):")
            lines += ["    %s" % n for n in names] or ["    (nobody yet)"]
        self._message("Who's in the Room", "\n".join(lines))


_mode = None


def createMode():
    global _mode
    _mode = PMFRoomMode()
    return _mode
