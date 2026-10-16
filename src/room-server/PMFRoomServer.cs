using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

// PMF.RV Room Server -- a small rendezvous + relay for PMF.RV Room.
//
// Why it exists: Sync Review is peer-to-peer TCP, so a participant can only
// join if their machine can open an INBOUND connection to the host. That
// fails across subnets, Wi-Fi vs Ethernet, NAT, switch client isolation, and
// any machine whose firewall blocks inbound -- and UDP broadcast discovery
// never leaves the local subnet at all. With this relay, every participant
// (host included) only makes OUTBOUND connections to one internal machine
// that everyone can reach, and the relay pipes the bytes. RV's own protocol
// passes through untouched.
//
// Protocol (one header line, '\n'-terminated, then raw bytes):
//   HOST <code> [name]  host's control channel; relay answers OK / ERR <why>,
//                       then sends "OPEN <sid>" per joiner; host sends PING.
//   CHECK <code>        OK if the room exists, else ERR noroom.
//   JOIN <code> [name]  joiner's tunnel; relay asks the host to open a
//                       matching DATA connection, answers OK, then pipes
//                       both ways.
//   DATA <sid>          host's side of one joiner tunnel.
//   MEMBERS <code>      "OK host=<name> in=<a,b> left=<c,d>" -- who is in the
//                       room now and who has left. Anyone with the code can
//                       ask, so every participant (not only the host) sees
//                       the whole room. RV's own Contacts list can't: in a
//                       relayed room each guest is only connected to the host.
//
// PMF.RV Notify presence (names are profile names, e.g. Alex_EMP0485):
//   PRESENCE <name>     long-lived connection from a PC's PMFNotify tray
//                       app; OK, then client PING / relay PONG, and the relay
//                       pushes "NOTIFY <from>" lines down it.
//   NOTIFY <name> <from...>  delivers to every PC signed in as <name>:
//                       OK, or ERR offline.
//   WHO                 "OK a,b,c" -- names currently signed in.
// So people are notified by name, across any network that reaches the relay,
// instead of by IP address or computer name.
//
// [name] is optional so older PMF.RV versions keep working; they show up as
// "guest".
//
// Security: room codes must match ^[a-z0-9-]{4,64}$; failed lookups are
// rate-limited per source IP; session ids are 128-bit random; a DATA
// connection is only accepted from the same IP as that room's host. The
// relay is blind -- it never parses RV traffic -- and RV traffic is not
// encrypted (same as direct Sync Review), so only expose it on internal
// networks (the rule --install creates is scoped to private ranges +
// Tailscale).
//
// Install on the ONE always-on machine that hosts rooms:
//   PMFRoomServer.exe --install     (one UAC prompt)
// copies it to %ProgramData%\PMF.RV\RoomServer, adds its firewall rule,
// starts it, and makes it start at login. --uninstall reverses all of it.
// It is not part of the client install that colleagues update.
[assembly: System.Reflection.AssemblyTitle("PMF.RV Room Server")]
[assembly: System.Reflection.AssemblyDescription("PMF.RV Room rendezvous and relay")]
[assembly: System.Reflection.AssemblyProduct("PMF.RV")]
[assembly: System.Reflection.AssemblyVersion("1.2.0.0")]

class Room
{
    public string Code;
    public Socket Control;
    public string HostIp;
    public string HostName = "host";
    // joiner tunnel socket -> name; and names whose last tunnel closed.
    // Guarded by Relay.Sync.
    public readonly Dictionary<Socket, string> Members = new Dictionary<Socket, string>();
    public readonly List<string> Left = new List<string>();
    public readonly object WriteLock = new object();
    public readonly List<Socket> Sockets = new List<Socket>();
    public DateTime Created = DateTime.Now;
}

class Presence
{
    public string Name;
    public Socket Socket;
    public readonly object WriteLock = new object();
}

class PendingTunnel
{
    public Room Room;
    public Socket HostData;
    public readonly ManualResetEvent Ready = new ManualResetEvent(false);
}

static class Relay
{
    public const int DEFAULT_PORT = 45150;
    const int MAX_HEADER = 512;
    const int MAX_PRESENCE = 2000;
    const int MAX_PRESENCE_PER_NAME = 8;   // same person signed in on several PCs
    const int NOTIFY_LIMIT_PER_MIN = 20;   // per source IP
    const int MAX_ROOMS = 200;
    const int MAX_TUNNELS_PER_ROOM = 32;
    const int HEADER_TIMEOUT_MS = 10000;
    const int CONTROL_IDLE_MS = 70000;   // host pings every 20 s
    const int HOST_OPEN_TIMEOUT_MS = 15000;
    const int FAIL_LIMIT = 20;           // failed lookups per window
    const int FAIL_WINDOW_S = 60;
    const int BLOCK_S = 300;

    static readonly Regex CodeRe = new Regex("^[a-z0-9-]{4,64}$");
    static readonly Regex SidRe = new Regex("^[a-f0-9]{32}$");
    static readonly Regex NameRe = new Regex("^[A-Za-z0-9_-]{1,64}$");

    static readonly object Sync = new object();
    static readonly Dictionary<string, Room> Rooms = new Dictionary<string, Room>();
    static readonly Dictionary<string, PendingTunnel> Pending = new Dictionary<string, PendingTunnel>();
    static readonly Dictionary<string, List<KeyValuePair<DateTime, string>>> Failures =
        new Dictionary<string, List<KeyValuePair<DateTime, string>>>();
    static readonly Dictionary<string, DateTime> BlockedUntil = new Dictionary<string, DateTime>();
    // Codes whose host said BYE (deliberate close), so lookups can answer
    // "ERR closed" instead of "ERR noroom". Guests auto-rejoining after a
    // drop must keep retrying on noroom (a restarted relay forgets rooms
    // until hosts re-register) but should stop at once on a real close.
    static readonly Dictionary<string, DateTime> ClosedByHost = new Dictionary<string, DateTime>();
    const int CLOSED_MEMORY_S = 600;
    static readonly RandomNumberGenerator Rng = RandomNumberGenerator.Create();
    static readonly Dictionary<string, List<Presence>> Online = new Dictionary<string, List<Presence>>(StringComparer.OrdinalIgnoreCase);
    static readonly Dictionary<string, List<DateTime>> NotifyTimes = new Dictionary<string, List<DateTime>>();

    static TcpListener listener;
    static string logPath;
    static readonly object LogLock = new object();

    public static int RoomCount { get { lock (Sync) return Rooms.Count; } }
    public static int OnlineCount { get { lock (Sync) return Online.Count; } }
    public static int TunnelCount
    {
        get
        {
            lock (Sync)
            {
                int n = 0;
                foreach (var r in Rooms.Values) n += r.Sockets.Count / 2;
                return n;
            }
        }
    }

    public static void Start(int port, string logFile)
    {
        logPath = logFile;
        listener = new TcpListener(IPAddress.Any, port);
        listener.Start();
        Log("Listening on TCP " + port);
        var t = new Thread(AcceptLoop) { IsBackground = true };
        t.Start();
    }

    public static void Stop()
    {
        try { listener.Stop(); } catch { }
    }

    public static void Log(string msg)
    {
        string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg;
        lock (LogLock)
        {
            try
            {
                if (File.Exists(logPath) && new FileInfo(logPath).Length > 5 * 1024 * 1024)
                    File.Delete(logPath);
                File.AppendAllText(logPath, line + Environment.NewLine);
            }
            catch { }
        }
    }

    static void AcceptLoop()
    {
        while (true)
        {
            Socket s;
            try { s = listener.AcceptSocket(); }
            catch { return; }
            var t = new Thread(() => HandleClient(s)) { IsBackground = true };
            t.Start();
        }
    }

    static string RemoteIp(Socket s)
    {
        try { return ((IPEndPoint)s.RemoteEndPoint).Address.ToString(); }
        catch { return "?"; }
    }

    static void HandleClient(Socket s)
    {
        string ip = RemoteIp(s);
        bool handedOff = false;
        try
        {
            s.NoDelay = true;
            s.ReceiveTimeout = HEADER_TIMEOUT_MS;

            // Read the request before any refusal: closing a socket that
            // still has unread input makes Windows send RST, which discards
            // the error line we just sent and the client never learns why.
            string line = ReadLine(s);
            if (line == null) return;

            if (IsBlocked(ip))
            {
                SendLine(s, "ERR ratelimited");
                return;
            }
            string[] p = line.Trim().Split(' ');

            switch (p[0])
            {
                case "HOST":     HandleHost(s, ip, p); break;
                case "CHECK":    HandleCheck(s, ip, p); break;
                case "JOIN":     HandleJoin(s, ip, p); break;
                case "DATA":     handedOff = HandleData(s, ip, p); break;
                case "MEMBERS":  HandleMembers(s, ip, p); break;
                case "PRESENCE": HandlePresence(s, ip, p); break;
                case "NOTIFY":   HandleNotify(s, ip, line.Trim()); break;
                case "WHO":      HandleWho(s); break;
                default:
                    RecordFailure(ip);
                    SendLine(s, "ERR badcommand");
                    break;
            }
        }
        catch (Exception e)
        {
            Log("Connection from " + ip + " failed: " + e.Message);
        }
        finally
        {
            if (!handedOff) CloseQuiet(s);
        }
    }

    static void HandleHost(Socket s, string ip, string[] p)
    {
        if (p.Length < 2 || p.Length > 3 || !CodeRe.IsMatch(p[1])) { RecordFailure(ip); SendLine(s, "ERR badcode"); return; }
        string code = p[1];

        var room = new Room { Code = code, Control = s, HostIp = ip, HostName = NameOrDefault(p, 2, "host") };
        lock (Sync)
        {
            if (Rooms.Count >= MAX_ROOMS) { SendLine(s, "ERR serverfull"); return; }
            if (Rooms.ContainsKey(code)) { SendLine(s, "ERR taken"); return; }
            Rooms[code] = room;
            ClosedByHost.Remove(code);
        }

        Log("Room '" + code + "' opened by " + room.HostName + " (" + ip + ")");
        try
        {
            lock (room.WriteLock) SendLine(s, "OK");
            s.ReceiveTimeout = CONTROL_IDLE_MS;

            while (true)
            {
                string cmd = ReadLine(s);
                if (cmd == null) break;
                if (cmd == "BYE")
                {
                    lock (Sync) ClosedByHost[code] = DateTime.Now;
                    break;
                }
                if (cmd == "PING")
                    lock (room.WriteLock) SendLine(s, "PONG");
            }
        }
        catch (Exception e)
        {
            Log("Room '" + code + "' control channel ended: " + e.Message);
        }
        finally
        {
            CloseRoom(room);
        }
    }

    static void CloseRoom(Room room)
    {
        List<Socket> toClose;
        lock (Sync)
        {
            Room current;
            if (Rooms.TryGetValue(room.Code, out current) && current == room)
                Rooms.Remove(room.Code);
            toClose = new List<Socket>(room.Sockets);
            room.Sockets.Clear();

            var stale = new List<string>();
            foreach (var kv in Pending)
                if (kv.Value.Room == room) stale.Add(kv.Key);
            foreach (var sid in stale)
            {
                Pending[sid].Ready.Set();
                Pending.Remove(sid);
            }
        }
        foreach (var t in toClose) CloseQuiet(t);
        CloseQuiet(room.Control);
        Log("Room '" + room.Code + "' closed (" + toClose.Count / 2 + " tunnel(s) dropped)");
    }

    static void HandleCheck(Socket s, string ip, string[] p)
    {
        if (p.Length != 2 || !CodeRe.IsMatch(p[1])) { RecordFailure(ip); SendLine(s, "ERR badcode"); return; }
        bool exists;
        lock (Sync) exists = Rooms.ContainsKey(p[1]);
        SendLine(s, exists ? "OK" : MissingRoomAnswer(p[1], ip));
    }

    // Caller holds no lock. "ERR closed" for a code its host closed in the
    // last CLOSED_MEMORY_S (a legitimate lookup, not counted as a failure);
    // otherwise "ERR noroom", which does count toward rate limiting.
    static string MissingRoomAnswer(string code, string ip)
    {
        lock (Sync)
        {
            DateTime when;
            if (ClosedByHost.TryGetValue(code, out when))
            {
                if ((DateTime.Now - when).TotalSeconds < CLOSED_MEMORY_S)
                    return "ERR closed";
                ClosedByHost.Remove(code);
            }
        }
        RecordFailure(ip, code);
        return "ERR noroom";
    }

    static void HandleJoin(Socket s, string ip, string[] p)
    {
        if (p.Length < 2 || p.Length > 3 || !CodeRe.IsMatch(p[1])) { RecordFailure(ip); SendLine(s, "ERR badcode"); return; }
        string code = p[1];
        string name = NameOrDefault(p, 2, "guest");

        Room room;
        string sid = NewSid();
        var pt = new PendingTunnel();
        lock (Sync)
        {
            if (!Rooms.TryGetValue(code, out room))
            {
                SendLine(s, MissingRoomAnswer(code, ip));  // C# lock is re-entrant
                return;
            }
            if (room.Sockets.Count / 2 >= MAX_TUNNELS_PER_ROOM)
            {
                SendLine(s, "ERR roomfull");
                return;
            }
            pt.Room = room;
            Pending[sid] = pt;
        }

        try
        {
            lock (room.WriteLock) SendLine(room.Control, "OPEN " + sid);
        }
        catch
        {
            lock (Sync) Pending.Remove(sid);
            SendLine(s, "ERR hostgone");
            return;
        }

        pt.Ready.WaitOne(HOST_OPEN_TIMEOUT_MS);

        Socket hostData;
        lock (Sync)
        {
            Pending.Remove(sid);
            hostData = pt.HostData;
            Room current;
            bool roomAlive = Rooms.TryGetValue(code, out current) && current == room;
            if (hostData != null && !roomAlive)
            {
                CloseQuiet(hostData);
                hostData = null;
            }
            if (hostData != null)
            {
                room.Sockets.Add(s);
                room.Sockets.Add(hostData);
                room.Members[s] = name;
                room.Left.RemoveAll(n => n == name);
            }
        }

        if (hostData == null)
        {
            SendLine(s, "ERR hosttimeout");
            Log("Join to '" + code + "' from " + ip + " timed out waiting for host");
            return;
        }

        Log("Tunnel " + sid.Substring(0, 8) + " open: " + name + " (" + ip + ") -> room '" + code + "'");
        try
        {
            SendLine(s, "OK");
            s.ReceiveTimeout = 0;
            hostData.ReceiveTimeout = 0;
            hostData.NoDelay = true;

            var back = new Thread(() => Pump(hostData, s)) { IsBackground = true };
            back.Start();
            Pump(s, hostData);
            back.Join(5000);
        }
        finally
        {
            lock (Sync)
            {
                room.Sockets.Remove(s);
                room.Sockets.Remove(hostData);
                room.Members.Remove(s);
                if (!room.Members.ContainsValue(name))
                {
                    room.Left.RemoveAll(n => n == name);
                    room.Left.Add(name);
                    if (room.Left.Count > 64) room.Left.RemoveAt(0);
                }
            }
            CloseQuiet(hostData);
            Log("Tunnel " + sid.Substring(0, 8) + " closed");
        }
    }

    static string NameOrDefault(string[] p, int i, string fallback)
    {
        return p.Length > i && NameRe.IsMatch(p[i]) ? p[i] : fallback;
    }

    static void HandleMembers(Socket s, string ip, string[] p)
    {
        if (p.Length != 2 || !CodeRe.IsMatch(p[1])) { RecordFailure(ip); SendLine(s, "ERR badcode"); return; }
        string answer = null;
        lock (Sync)
        {
            Room room;
            if (Rooms.TryGetValue(p[1], out room))
            {
                var inRoom = new List<string>();
                foreach (string n in room.Members.Values)
                    if (!inRoom.Contains(n)) inRoom.Add(n);
                inRoom.Sort(StringComparer.OrdinalIgnoreCase);
                answer = "OK host=" + room.HostName + " in=" + string.Join(",", inRoom.ToArray())
                    + " left=" + string.Join(",", room.Left.ToArray());
            }
        }
        SendLine(s, answer ?? MissingRoomAnswer(p[1], ip));
    }

    static void HandlePresence(Socket s, string ip, string[] p)
    {
        if (p.Length != 2 || !NameRe.IsMatch(p[1])) { RecordFailure(ip); SendLine(s, "ERR badname"); return; }
        var me = new Presence { Name = p[1], Socket = s };
        lock (Sync)
        {
            if (Online.Count >= MAX_PRESENCE) { SendLine(s, "ERR serverfull"); return; }
            List<Presence> list;
            if (!Online.TryGetValue(me.Name, out list)) Online[me.Name] = list = new List<Presence>();
            if (list.Count >= MAX_PRESENCE_PER_NAME)
            {
                CloseQuiet(list[0].Socket);   // oldest sign-in under this name gives way
                list.RemoveAt(0);
            }
            list.Add(me);
        }
        try
        {
            lock (me.WriteLock) SendLine(s, "OK");
            s.ReceiveTimeout = CONTROL_IDLE_MS;
            while (true)
            {
                string cmd = ReadLine(s);
                if (cmd == null || cmd == "BYE") break;
                if (cmd == "PING")
                    lock (me.WriteLock) SendLine(s, "PONG");
            }
        }
        catch { }
        finally
        {
            lock (Sync)
            {
                List<Presence> list;
                if (Online.TryGetValue(me.Name, out list))
                {
                    list.Remove(me);
                    if (list.Count == 0) Online.Remove(me.Name);
                }
            }
        }
    }

    static void HandleNotify(Socket s, string ip, string line)
    {
        // NOTIFY <name> <free text: who is asking, e.g. "Alex (EMP0485)">
        string[] p = line.Split(new[] { ' ' }, 3);
        if (p.Length < 2 || !NameRe.IsMatch(p[1])) { RecordFailure(ip); SendLine(s, "ERR badname"); return; }
        string from = p.Length > 2 ? Regex.Replace(p[2], @"[\x00-\x1f\x7f]", "").Trim() : "";
        if (from.Length > 80) from = from.Substring(0, 80);
        if (from.Length == 0) from = "Someone";

        List<Presence> targets;
        lock (Sync)
        {
            List<DateTime> times;
            if (!NotifyTimes.TryGetValue(ip, out times)) NotifyTimes[ip] = times = new List<DateTime>();
            var now = DateTime.Now;
            times.RemoveAll(t => (now - t).TotalSeconds > 60);
            if (times.Count >= NOTIFY_LIMIT_PER_MIN) { SendLine(s, "ERR ratelimited"); return; }
            times.Add(now);

            List<Presence> list;
            targets = Online.TryGetValue(p[1], out list) ? new List<Presence>(list) : new List<Presence>();
        }

        int delivered = 0;
        foreach (var t in targets)
        {
            try
            {
                lock (t.WriteLock) SendLine(t.Socket, "NOTIFY " + from);
                delivered++;
            }
            catch { }
        }
        Log("Notify " + p[1] + " from '" + from + "' (" + ip + "): " + (delivered > 0 ? "delivered to " + delivered + " PC(s)" : "offline"));
        SendLine(s, delivered > 0 ? "OK" : "ERR offline");
    }

    static void HandleWho(Socket s)
    {
        List<string> names;
        lock (Sync) names = new List<string>(Online.Keys);
        names.Sort(StringComparer.OrdinalIgnoreCase);
        SendLine(s, "OK " + string.Join(",", names.ToArray()));
    }

    // Returns true when ownership of the socket moved to a joiner thread.
    static bool HandleData(Socket s, string ip, string[] p)
    {
        if (p.Length != 2 || !SidRe.IsMatch(p[1])) { RecordFailure(ip); return false; }
        lock (Sync)
        {
            PendingTunnel pt;
            if (!Pending.TryGetValue(p[1], out pt) || pt.HostData != null)
                return false;
            if (pt.Room.HostIp != ip)
            {
                Log("Rejected DATA for room '" + pt.Room.Code + "' from " + ip + " (host is " + pt.Room.HostIp + ")");
                return false;
            }
            pt.HostData = s;
            pt.Ready.Set();
            return true;
        }
    }

    static void Pump(Socket from, Socket to)
    {
        var buf = new byte[65536];
        try
        {
            while (true)
            {
                int n = from.Receive(buf);
                if (n <= 0) break;
                int off = 0;
                while (off < n) off += to.Send(buf, off, n - off, SocketFlags.None);
            }
        }
        catch { }
        finally
        {
            try { from.Shutdown(SocketShutdown.Both); } catch { }
            try { to.Shutdown(SocketShutdown.Both); } catch { }
        }
    }

    // Byte-at-a-time so nothing after the '\n' is consumed -- those bytes
    // belong to the RV stream that follows the header.
    static string ReadLine(Socket s)
    {
        var bytes = new List<byte>();
        var one = new byte[1];
        while (true)
        {
            int n = s.Receive(one);
            if (n <= 0) return null;
            if (one[0] == (byte)'\n') break;
            if (one[0] != (byte)'\r') bytes.Add(one[0]);
            if (bytes.Count > MAX_HEADER) return null;
        }
        return Encoding.UTF8.GetString(bytes.ToArray());
    }

    static void SendLine(Socket s, string line)
    {
        var b = Encoding.UTF8.GetBytes(line + "\n");
        s.Send(b);
    }

    static void CloseQuiet(Socket s)
    {
        if (s == null) return;
        try { s.Shutdown(SocketShutdown.Both); } catch { }
        try { s.Close(); } catch { }
    }

    static string NewSid()
    {
        var b = new byte[16];
        Rng.GetBytes(b);
        var sb = new StringBuilder();
        foreach (var x in b) sb.Append(x.ToString("x2"));
        return sb.ToString();
    }

    // Counts DISTINCT failed codes per source IP. Guessing means trying many
    // different codes; legitimate auto-rejoin retries the SAME code while a
    // host re-registers after a relay restart -- and many PCs behind one NAT
    // address must not be able to lock each other out that way.
    static void RecordFailure(string ip, string code = null)
    {
        lock (Sync)
        {
            List<KeyValuePair<DateTime, string>> list;
            if (!Failures.TryGetValue(ip, out list)) Failures[ip] = list = new List<KeyValuePair<DateTime, string>>();
            var now = DateTime.Now;
            list.Add(new KeyValuePair<DateTime, string>(now, code ?? ("#" + NewSid())));
            list.RemoveAll(e => (now - e.Key).TotalSeconds > FAIL_WINDOW_S);
            var distinct = new HashSet<string>();
            foreach (var e in list) distinct.Add(e.Value);
            if (distinct.Count >= FAIL_LIMIT)
            {
                BlockedUntil[ip] = now.AddSeconds(BLOCK_S);
                list.Clear();
                Log("Rate-limited " + ip + " for " + BLOCK_S + " s (too many different failed codes)");
            }
        }
    }

    static bool IsBlocked(string ip)
    {
        lock (Sync)
        {
            DateTime until;
            if (BlockedUntil.TryGetValue(ip, out until))
            {
                if (DateTime.Now < until) return true;
                BlockedUntil.Remove(ip);
            }
            return false;
        }
    }
}

class RoomServerApp : ApplicationContext
{
    NotifyIcon tray;
    System.Windows.Forms.Timer statusTimer;
    string logPath;

    [STAThread]
    static int Main(string[] args)
    {
        int port = Relay.DEFAULT_PORT;
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == "--port") int.TryParse(args[i + 1], out port);

        if (Array.IndexOf(args, "--install") >= 0) return Installer.Install(port);
        if (Array.IndexOf(args, "--uninstall") >= 0) return Installer.Uninstall();
        int parent = 0;
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == "--parent") int.TryParse(args[i + 1], out parent);
        if (Array.IndexOf(args, "--elevated-install") >= 0) return Installer.ElevatedInstall(port, parent);
        if (Array.IndexOf(args, "--elevated-uninstall") >= 0) return Installer.ElevatedUninstall(parent);

        bool createdNew;
        var mutex = new Mutex(true, "PMF_RV_RoomServer_SingleInstance_" + port, out createdNew);
        if (!createdNew) return 0;

        string self = Path.GetDirectoryName(Application.ExecutablePath);
        string logPath = Path.Combine(self, "room_server_log.txt");

        try
        {
            Relay.Start(port, logPath);
        }
        catch (Exception e)
        {
            MessageBox.Show("PMF.RV Room Server could not listen on port " + port + ":\n\n" + e.Message,
                "PMF.RV Room Server", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        Application.EnableVisualStyles();
        Application.Run(new RoomServerApp(self, logPath, port));
        GC.KeepAlive(mutex);
        return 0;
    }

    public RoomServerApp(string self, string logPath, int port)
    {
        this.logPath = logPath;
        tray = new NotifyIcon();
        try { tray.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
        catch { tray.Icon = SystemIcons.Application; }
        tray.Visible = true;

        var menu = new ContextMenuStrip();
        menu.Items.Add("PMF.RV Room Server (port " + port + ")").Enabled = false;
        menu.Items.Add("Open log", null, (s, e) => { try { Process.Start(logPath); } catch { } });
        menu.Items.Add("-");
        menu.Items.Add("Exit", null, (s, e) => { Relay.Stop(); tray.Visible = false; Application.Exit(); });
        tray.ContextMenuStrip = menu;

        statusTimer = new System.Windows.Forms.Timer { Interval = 2000 };
        statusTimer.Tick += (s, e) =>
        {
            string t = "PMF.RV Room Server: " + Relay.RoomCount + " room(s), " + Relay.TunnelCount + " link(s), " + Relay.OnlineCount + " online";
            tray.Text = t.Length > 63 ? t.Substring(0, 63) : t;
        };
        statusTimer.Start();
    }
}

// --install / --uninstall. The live server runs from a fixed location
// outside any PMF.RVTOOL folder, so rebuilding or moving the dev folder,
// or an auto-update, can never stop or lock the relay everyone is using.
// The firewall rule is program-scoped to that copy AND to its port: a
// port-only rule would still let Windows prompt for the exe, and
// "Cancel" on that prompt creates a block rule that wins over any allow.
static class Installer
{
    const string RUN_VALUE = "PMF.RV Room Server";
    const string RULE_NAME = "PMF.RV Room Server";
    const string LEGACY_RULE_NAME = "PMF.RV Room Server (PMFRoomServer.exe)";
    const string TITLE = "PMF.RV Room Server";

    static string InstallDir
    {
        get
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                "PMF.RV", "RoomServer");
        }
    }

    static string TargetExe { get { return Path.Combine(InstallDir, "PMFRoomServer.exe"); } }

    // Firewall changes need admin; the Run key and starting the server must
    // happen as the logged-in user, so only this part is elevated.
    static int RunElevated(string args)
    {
        try
        {
            var psi = new ProcessStartInfo(Application.ExecutablePath, args) { UseShellExecute = true, Verb = "runas" };
            using (var p = Process.Start(psi))
            {
                p.WaitForExit();
                return p.ExitCode;
            }
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return -1; // UAC prompt cancelled
        }
    }

    public static int Install(int port)
    {
        int rc = RunElevated("--elevated-install --port " + port + " --parent " + Process.GetCurrentProcess().Id);
        if (rc != 0)
        {
            MessageBox.Show("Installation did not complete (code " + rc + ")." + (rc == -1 ? "\n\nThe administrator prompt was cancelled." : ""),
                TITLE, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        string args = port == Relay.DEFAULT_PORT ? "" : " --port " + port;
        using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
            if (key != null)
                key.SetValue(RUN_VALUE, "\"" + TargetExe + "\"" + args);

        Process.Start(new ProcessStartInfo(TargetExe, args.Trim()) { UseShellExecute = true, WorkingDirectory = InstallDir });
        MessageBox.Show("PMF.RV Room Server is installed and running.\n\n"
            + "Location:  " + TargetExe + "\n"
            + "Port:  TCP " + port + " (private networks + Tailscale only)\n"
            + "Starts automatically when you log in.",
            TITLE, MessageBoxButtons.OK, MessageBoxIcon.Information);
        return 0;
    }

    public static int Uninstall()
    {
        int rc = RunElevated("--elevated-uninstall --parent " + Process.GetCurrentProcess().Id);
        using (var key = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", true))
            if (key != null && key.GetValue(RUN_VALUE) != null)
                key.DeleteValue(RUN_VALUE);
        MessageBox.Show(rc == 0 ? "PMF.RV Room Server was removed." : "Removal did not complete (code " + rc + ").",
            TITLE, MessageBoxButtons.OK, rc == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Error);
        return rc == 0 ? 0 : 1;
    }

    // Stops running relays -- but not this process, nor the non-elevated
    // installer that launched it (same exe name); killing that one left the
    // Run key and the start-up of the installed copy undone.
    static void StopOtherServers(int parent)
    {
        foreach (Process p in Process.GetProcessesByName("PMFRoomServer"))
        {
            if (p.Id == Process.GetCurrentProcess().Id || p.Id == parent)
                continue;
            try { p.Kill(); p.WaitForExit(5000); } catch { }
        }
    }

    static int Netsh(string args)
    {
        var psi = new ProcessStartInfo("netsh", args) { UseShellExecute = false, CreateNoWindow = true };
        using (var p = Process.Start(psi))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }

    public static int ElevatedInstall(int port, int parent)
    {
        try
        {
            StopOtherServers(parent);
            Directory.CreateDirectory(InstallDir);
            if (!string.Equals(Path.GetFullPath(Application.ExecutablePath), Path.GetFullPath(TargetExe), StringComparison.OrdinalIgnoreCase))
                File.Copy(Application.ExecutablePath, TargetExe, true);

            Netsh("advfirewall firewall delete rule name=\"" + LEGACY_RULE_NAME + "\"");
            Netsh("advfirewall firewall delete rule name=\"" + RULE_NAME + "\"");
            return Netsh("advfirewall firewall add rule name=\"" + RULE_NAME + "\" dir=in action=allow"
                + " program=\"" + TargetExe + "\" protocol=TCP localport=" + port
                + " enable=yes profile=private,domain,public"
                + " remoteip=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10") == 0 ? 0 : 3;
        }
        catch
        {
            return 2;
        }
    }

    public static int ElevatedUninstall(int parent)
    {
        try
        {
            StopOtherServers(parent);
            Netsh("advfirewall firewall delete rule name=\"" + RULE_NAME + "\"");
            Netsh("advfirewall firewall delete rule name=\"" + LEGACY_RULE_NAME + "\"");
            if (Directory.Exists(InstallDir))
                Directory.Delete(InstallDir, true);
            return 0;
        }
        catch
        {
            return 2;
        }
    }
}
