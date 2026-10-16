"""Protocol-level tests for PMFRoomServer.exe (no RV involved)."""
import socket, threading, time, sys

RELAY = (sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 45150)
results = []

def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + detail) if detail else ""))

def conn(addr=RELAY):
    s = socket.create_connection(addr, timeout=5)
    s.settimeout(5)
    return s

def send_line(s, line):
    s.sendall((line + "\n").encode())

def read_line(s):
    buf = b""
    while True:
        c = s.recv(1)
        if not c:
            return None
        if c == b"\n":
            return buf.decode()
        buf += c

def recv_exact(s, n):
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data

code = "test-relay-%d" % int(time.time())

# 1. HOST registers
host = conn(); send_line(host, "HOST " + code)
check("HOST registers room", read_line(host) == "OK")

# 2. duplicate HOST rejected
dup = conn(); send_line(dup, "HOST " + code)
r = read_line(dup); dup.close()
check("duplicate HOST rejected", r == "ERR taken", r)

# 3. CHECK
c = conn(); send_line(c, "CHECK " + code); r = read_line(c); c.close()
check("CHECK existing room", r == "OK", r)
c = conn(); send_line(c, "CHECK no-such-room-1"); r = read_line(c); c.close()
check("CHECK missing room", r == "ERR noroom", r)

# 4. bad code / bad command
c = conn(); send_line(c, "CHECK Bad Code!"); r = read_line(c); c.close()
check("malformed code rejected", r == "ERR badcode", r)

# 5. PING keeps control alive
send_line(host, "PING")
check("PING answered", read_line(host) == "PONG")

# 6. JOIN + host DATA + bidirectional piping (incl. bytes sent before OK)
host_side = {}
def host_responder():
    line = read_line(host)
    host_side["open"] = line
    sid = line.split(" ")[1]
    d = conn(); send_line(d, "DATA " + sid)
    d.sendall(b"HOST-GREETING")          # host side may speak first
    host_side["got"] = recv_exact(d, 13)  # expects JOINER-HELLO!
    d.sendall(b"ECHO:" + host_side["got"])
    host_side["sock"] = d

t = threading.Thread(target=host_responder); t.start()
j = conn(); send_line(j, "JOIN " + code)
r = read_line(j)
check("JOIN accepted", r == "OK", r)
j.sendall(b"JOINER-HELLO!")
first = recv_exact(j, 13)
echo = recv_exact(j, 18)
t.join(5)
check("relay sent OPEN to host", (host_side.get("open") or "").startswith("OPEN "))
check("host->joiner bytes delivered", first == b"HOST-GREETING", repr(first))
check("joiner->host bytes delivered", host_side.get("got") == b"JOINER-HELLO!", repr(host_side.get("got")))
check("round trip echo", echo == b"ECHO:JOINER-HELLO!", repr(echo))

# 7. large payload integrity (1 MB)
payload = bytes(range(256)) * 4096
d = host_side["sock"]
got = {}
def reader():
    got["data"] = recv_exact(d, len(payload))
rt = threading.Thread(target=reader); rt.start()
j.sendall(payload); rt.join(15)
check("1 MB payload intact through relay", got.get("data") == payload)

# 8. DATA with unknown sid is dropped
x = conn(); send_line(x, "DATA " + "0" * 32)
x.settimeout(3)
try:
    closed = x.recv(1) == b""
except Exception:
    closed = False
x.close()
check("unknown DATA sid dropped", closed)

# 9. closing host control closes room AND live tunnels
send_line(host, "BYE"); host.close()
time.sleep(1)
j.settimeout(3)
try:
    tunnel_closed = j.recv(1) == b""
except Exception:
    tunnel_closed = False
check("host leaving drops joiner tunnel", tunnel_closed)
c = conn(); send_line(c, "CHECK " + code); r = read_line(c); c.close()
check("deliberate close reported as closed", r == "ERR closed", r)

# 10. JOIN when host never answers OPEN -> ERR hosttimeout (15 s)
code2 = code + "-b"
h2 = conn(); send_line(h2, "HOST " + code2); read_line(h2)
j2 = conn(); j2.settimeout(20); send_line(j2, "JOIN " + code2)
t0 = time.time(); r = read_line(j2); dt = time.time() - t0
check("unanswered OPEN times out cleanly", r == "ERR hosttimeout", "%s after %.1fs" % (r, dt))
j2.close(); h2.close()

print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))
