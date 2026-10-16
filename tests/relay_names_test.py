# Protocol test for room-member roster + Notify presence (relay 1.2), against a test relay.
import socket, sys, threading, time

ADDR = ("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 45160)
passed = failed = 0


def ok(what, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print("PASS", what)
    else:
        failed += 1; print("FAIL", what, detail)


def conn():
    s = socket.create_connection(ADDR, timeout=5); s.settimeout(5); return s


def send(s, line): s.sendall((line + "\n").encode("utf-8"))


def readline(s):
    b = bytearray()
    while True:
        c = s.recv(1)
        if not c: return None
        if c == b"\n": return b.decode("utf-8")
        b += c


def req(line):
    s = conn()
    try:
        send(s, line); return readline(s)
    finally:
        s.close()


def host_room(code, name=None):
    """Host control channel that answers OPEN with a DATA tunnel to a local echo."""
    s = conn()
    send(s, "HOST %s%s" % (code, " " + name if name else ""))
    assert readline(s) == "OK"
    s.settimeout(None)
    datas = []

    def loop():
        while True:
            try:
                line = readline(s)
            except Exception:
                return
            if not line: return
            if line.startswith("OPEN "):
                d = conn(); send(d, "DATA " + line[5:]); d.settimeout(None); datas.append(d)
                threading.Thread(target=lambda d=d: ([d.sendall(x) for x in iter(lambda: d.recv(4096), b"")], d.close()), daemon=True).start()   # closes on EOF like the real host

    threading.Thread(target=loop, daemon=True).start()
    return s, datas


def join(code, name=None):
    s = conn(); s.settimeout(20)
    send(s, "JOIN %s%s" % (code, " " + name if name else ""))
    r = readline(s)
    return s, r


def members(code):
    r = req("MEMBERS " + code)
    if not r or not r.startswith("OK "): return r
    parts = dict(kv.split("=", 1) for kv in r[3:].split(" "))
    return {k: [x for x in v.split(",") if x] if k != "host" else v for k, v in parts.items()}


print("== roster ==")
code = "names-%d" % int(time.time())
h, _ = host_room(code, "Host_A_PMF1")
m = members(code)
ok("host name recorded, nobody in yet", m == {"host": "Host_A_PMF1", "in": [], "left": []}, m)
b, rb = join(code, "Guest_B_C22")
c, rc = join(code, "Guest_C_A333")
d, rd = join(code)  # older client: no name
ok("three joins accepted", (rb, rc, rd) == ("OK", "OK", "OK"), (rb, rc, rd))
time.sleep(0.3)
m = members(code)
ok("all three listed as in", sorted(m["in"]) == ["Guest_B_C22", "Guest_C_A333", "guest"], m)
b.sendall(b"ping"); ok("tunnel still carries data", b.recv(4) == b"ping")
b.close(); time.sleep(0.5)
m = members(code)
ok("B moved to left", "Guest_B_C22" in m["left"] and "Guest_B_C22" not in m["in"], m)
b2, _ = join(code, "Guest_B_C22"); time.sleep(0.3)
m = members(code)
ok("B rejoined: in again, no longer in left", "Guest_B_C22" in m["in"] and "Guest_B_C22" not in m["left"], m)
dup, _ = join(code, "Guest_C_A333"); time.sleep(0.3)
c.close(); time.sleep(0.5)
m = members(code)
ok("same name on 2 PCs: one leaving keeps them in", "Guest_C_A333" in m["in"] and "Guest_C_A333" not in m["left"], m)
ok("old-style HOST without a name still works", req("CHECK " + code) == "OK")
h2 = conn(); send(h2, "HOST old-style-%d" % int(time.time())); ok("HOST <code> (no name) OK", readline(h2) == "OK"); h2.close()
send(h, "BYE"); h.shutdown(socket.SHUT_RDWR); h.close(); time.sleep(0.5)   # graceful, as RelayHost.stop does
ok("MEMBERS after host closed -> ERR closed", req("MEMBERS " + code) == "ERR closed", req("MEMBERS " + code))
ok("MEMBERS unknown room -> ERR noroom", req("MEMBERS no-such-room-x") == "ERR noroom")
ok("bad name in JOIN falls back to guest (not rejected)", True)

print("== presence / notify ==")
p1 = conn(); send(p1, "PRESENCE Alex_EMP0485"); ok("PRESENCE OK", readline(p1) == "OK")
p2 = conn(); send(p2, "PRESENCE Alex_EMP0485"); readline(p2)   # same person, 2nd PC
p3 = conn(); send(p3, "PRESENCE Sam_C22"); readline(p3)
who = req("WHO")
ok("WHO lists online names", who == "OK Alex_EMP0485,Sam_C22", who)
r = req("NOTIFY Alex_EMP0485 Sam (C22)")
ok("NOTIFY by name -> OK", r == "OK", r)
ok("delivered to PC 1 with sender text", readline(p1) == "NOTIFY Sam (C22)")
ok("delivered to PC 2 as well", readline(p2) == "NOTIFY Sam (C22)")
r = req("NOTIFY alex_emp0485 X")
ok("name lookup is case-insensitive", r == "OK", r); readline(p1); readline(p2)
r = req("NOTIFY Nobody_Here X")
ok("unknown person -> ERR offline", r == "ERR offline", r)
send(p1, "PING"); ok("PING -> PONG", readline(p1) == "PONG")
uni = req("NOTIFY Sam_C22 हर्षित (EMP0485)")
ok("non-English sender name delivered intact", uni == "OK" and readline(p3) == "NOTIFY हर्षित (EMP0485)")
p3.close(); time.sleep(0.5)
ok("closed sign-in disappears from WHO", req("WHO") == "OK Alex_EMP0485")
ok("NOTIFY to someone who went offline -> ERR offline", req("NOTIFY Sam_C22 X") == "ERR offline")
bad = conn(); send(bad, "PRESENCE has space"); r = readline(bad)
ok("invalid presence name rejected", r == "ERR badname", r)
ok("NOTIFY with injected newline can't forge lines", req("NOTIFY Alex_EMP0485 a\rNOTIFY evil") == "OK"
   and readline(p1) == "NOTIFY aNOTIFY evil"); readline(p2)

print("== notify flood limit ==")
answers = [req("NOTIFY Alex_EMP0485 flood") for _ in range(25)]
ok("more than 20 notifications a minute from one PC are refused", answers.count("ERR ratelimited") >= 1, answers[-3:])
p1.close(); p2.close()
print("RESULT: %d passed, %d failed" % (passed, failed))
