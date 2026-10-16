"""Security tests for PMFRoomServer.exe: DATA source-IP binding and rate limiting."""
import socket, sys, threading, time

# Needs two different source addresses: this PC's LAN address and loopback.
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 45150
LAN = (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else socket.gethostbyname(socket.gethostname()), PORT)
LOOP = ("127.0.0.1", PORT)
results = []

def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + detail) if detail else ""))

def conn(addr):
    s = socket.create_connection(addr, timeout=5); s.settimeout(5); return s

def send_line(s, line): s.sendall((line + "\n").encode())

def read_line(s):
    buf = b""
    while True:
        c = s.recv(1)
        if not c: return None
        if c == b"\n": return buf.decode()
        buf += c

# DATA from a different source IP than the host must be refused
code = "sec-%d" % int(time.time())
host = conn(LAN); send_line(host, "HOST " + code); read_line(host)   # host source = 192.0.2.10
hijack = {}
def attacker():
    sid = read_line(host).split(" ")[1]
    d = conn(LOOP); send_line(d, "DATA " + sid)                       # source = 127.0.0.1
    d.settimeout(3)
    try: hijack["closed"] = d.recv(1) == b""
    except Exception: hijack["closed"] = False
t = threading.Thread(target=attacker); t.start()
j = conn(LAN); j.settimeout(20); send_line(j, "JOIN " + code)
r = read_line(j); t.join(5)
check("DATA from foreign IP refused", hijack.get("closed") is True)
check("joiner not paired with hijacker", r == "ERR hosttimeout", r)
j.close(); host.close()

# retrying ONE missing code (auto-rejoin during a relay restart, possibly
# many PCs behind one NAT address) must never trip the limiter
same = []
for i in range(30):
    c = conn(LAN); send_line(c, "CHECK waiting-for-host-1"); same.append(read_line(c)); c.close()
check("30 retries of one code are not rate-limited", same[-1] == "ERR noroom", same[-1])

# rate limiting: 20 DIFFERENT failed codes in 60 s blocks that source IP
answers = []
for i in range(22):
    c = conn(LOOP); send_line(c, "CHECK nope-%d" % i); answers.append(read_line(c)); c.close()
check("first failures answered normally", answers[0] == "ERR noroom", answers[0])
check("source blocked after 20 failures", answers[-1] == "ERR ratelimited", answers[-1])
c = conn(LAN); send_line(c, "CHECK nope-x"); r = read_line(c); c.close()
check("other source IPs unaffected", r == "ERR noroom", r)

print("\n%d/%d passed" % (sum(results), len(results)))
