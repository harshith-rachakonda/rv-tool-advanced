using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

// PMF.RV Notify -- an always-on tray companion, separate from RV itself.
//
// RV's own network port only exists while RV is running -- there is no
// way to reach a closed application. This tiny app solves that the same
// way Teams/WhatsApp do: a lightweight background process starts with
// Windows and stays reachable regardless of whether the main app window
// is open, and clicking its notification brings the main app up.
//
// Two ways to reach it:
//   - By NAME (pmf.10.02+): it signs in to the PMF.RV room server under the
//     person's profile name (e.g. Alex_EMP0485, from
//     %APPDATA%\PMF.RV\profile.json) and keeps that connection open; the
//     server pushes "NOTIFY <from>" down it. Outbound only, so it works on
//     any network that reaches the room server -- no IP addresses to know.
//   - Directly on TCP 45140 ("PING:<from>"), for older PMF.RV versions and
//     when no room server is reachable (same network only).
//
// Invites: a sender text ending in " [room <code>]" is an invitation to that
// PMF.RV Room. Clicking the notification writes join_request.json next to
// the profile and opens PMF.RV -- or brings the running one to the front --
// and PMF.RV's Room plugin joins the room.
[assembly: System.Reflection.AssemblyTitle("PMF.RV Notify")]
[assembly: System.Reflection.AssemblyDescription("PMF.RV Presence/Notification Companion")]
[assembly: System.Reflection.AssemblyProduct("PMF.RV Notify")]
[assembly: System.Reflection.AssemblyVersion("1.2.0.0")]

class PMFNotifyApp : ApplicationContext
{
    // Deliberately separate from RV's own Sync Review ports (45124-45133)
    // -- this listener's whole reason to exist is being reachable when
    // RV/those ports are NOT active.
    const int NOTIFY_PORT = 45140;

    NotifyIcon trayIcon;
    TcpListener listener;
    Thread listenerThread;
    volatile bool running = true;
    string rvToolPath;
    string selfDir;
    SynchronizationContext uiContext;

    const string READY_TEXT = "PMF.RV Notify -- ready to receive review requests";

    // Last invite shown; used (once) by the next click. UI thread only.
    string pendingRoom, pendingFrom;
    DateTime pendingAt;
    static readonly Regex InviteRe = new Regex(@"^(.*) \[room ([a-z0-9-]{4,64})\]$");

    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);

    [STAThread]
    static void Main()
    {
        // Refuse a second copy -- it would just fail to bind the port
        // anyway, this just avoids a confusing duplicate tray icon.
        bool createdNew;
        var mutex = new Mutex(true, "PMF_RV_Notify_SingleInstance", out createdNew);
        if (!createdNew)
        {
            return;
        }

        Application.EnableVisualStyles();
        RegisterAutoStart();
        Application.Run(new PMFNotifyApp());
        GC.KeepAlive(mutex);
    }

    // Adds a per-user auto-start entry (HKCU Run key) so this behaves
    // like Teams/WhatsApp: install/run once, always available after
    // every login from then on, no separate "startup" step to remember.
    static void RegisterAutoStart()
    {
        try
        {
            string exePath = Application.ExecutablePath;
            using (var key = Registry.CurrentUser.OpenSubKey(
                "Software\\Microsoft\\Windows\\CurrentVersion\\Run", true))
            {
                if (key != null)
                {
                    string desired = "\"" + exePath + "\"";
                    object existing = key.GetValue("PMF.RV Notify");
                    if (existing == null || existing.ToString() != desired)
                    {
                        key.SetValue("PMF.RV Notify", desired);
                    }
                }
            }
        }
        catch { /* best-effort -- not fatal if this fails */ }
    }

    public PMFNotifyApp()
    {
        string self = Path.GetDirectoryName(Application.ExecutablePath);
        selfDir = self;
        rvToolPath = Path.Combine(self, "RVTool.exe");

        trayIcon = new NotifyIcon();
        try
        {
            trayIcon.Icon = new Icon(Path.Combine(self, "app", "resources", "RV.ico"));
        }
        catch
        {
            trayIcon.Icon = SystemIcons.Application;
        }
        trayIcon.Visible = true;
        trayIcon.Text = READY_TEXT;

        // Must capture AFTER the tray icon's native window handle exists
        // (Visible = true above creates it) -- that's what installs the
        // real WindowsFormsSynchronizationContext. Capturing this any
        // earlier (e.g. at the top of the constructor) grabs null, since
        // nothing has created a window handle yet at that point, which
        // made every PostNotification() call below throw silently.
        uiContext = SynchronizationContext.Current;

        var menu = new ContextMenuStrip();
        menu.Items.Add("Open RV", null, (s, e) => LaunchRV());
        menu.Items.Add("-");
        menu.Items.Add("Exit", null, (s, e) => ExitApp());
        trayIcon.ContextMenuStrip = menu;

        // Same interaction pattern as Teams/WhatsApp: click the
        // notification, single-click, or double-click the tray icon to
        // bring the app up. Windows 11 doesn't always route a click on
        // the Action-Center-collapsed version of a legacy balloon tip
        // back to BalloonTipClicked, so the tray icon itself needs to
        // work as a reliable fallback either way.
        trayIcon.BalloonTipClicked += (s, e) => LaunchRV();
        trayIcon.MouseClick += (s, e) => { if (e.Button == MouseButtons.Left) LaunchRV(); };
        trayIcon.MouseDoubleClick += (s, e) => LaunchRV();

        StartListening();

        var presence = new Thread(PresenceLoop) { IsBackground = true };
        presence.Start();
    }

    // ------------------------------------------------------------------
    // Signed in by name on the room server.
    // ------------------------------------------------------------------
    [DllImport("kernel32", CharSet = CharSet.Unicode)]
    static extern int GetPrivateProfileString(string section, string key, string def, StringBuilder ret, int size, string file);

    // Same rule as pmf_profile_mode.contact_name_for in PMF.RV, so the name
    // people pick in "Notify a Friend" is exactly the one signed in here.
    static string Clean(string value, int limit)
    {
        string t = Regex.Replace((value ?? "").Trim(), "[^A-Za-z0-9_-]+", "_");
        t = Regex.Replace(t, "_+", "_").Trim('_');
        return t.Length > limit ? t.Substring(0, limit) : t;
    }

    static string ProfileContactName()
    {
        try
        {
            string dir = Environment.GetEnvironmentVariable("PMF_RV_PROFILE_DIR");
            if (string.IsNullOrEmpty(dir))
                dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "PMF.RV");
            string path = Path.Combine(dir, "profile.json");
            if (!File.Exists(path))
                return null;   // PMF.RV asks for name + employee ID on first launch
            var d = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            object name, emp;
            d.TryGetValue("name", out name);
            d.TryGetValue("emp_id", out emp);
            if (name == null || emp == null || name.ToString().Trim().Length == 0 || emp.ToString().Trim().Length == 0)
                return null;
            string n = Clean(name.ToString(), 24), e = Clean(emp.ToString(), 32);
            string full = n.Length > 0 && e.Length > 0 ? n + "_" + e : n + e;
            return full.Length > 0 ? full : null;
        }
        catch
        {
            return null;
        }
    }

    List<KeyValuePair<string, int>> RoomServers()
    {
        var list = new List<KeyValuePair<string, int>>();
        var sb = new StringBuilder(2048);
        GetPrivateProfileString("room", "servers", "", sb, sb.Capacity, Path.Combine(selfDir, "app", "etc", "pmf_studio.ini"));
        foreach (string item in sb.ToString().Split(','))
        {
            string t = item.Trim();
            if (t.Length == 0)
                continue;
            int colon = t.LastIndexOf(':');
            int port = 45150;
            if (colon > 0 && int.TryParse(t.Substring(colon + 1), out port))
                t = t.Substring(0, colon);
            else
                port = 45150;
            list.Add(new KeyValuePair<string, int>(t.Trim(), port));
        }
        return list;
    }

    void SetTrayText(string text)
    {
        if (text.Length > 63)
            text = text.Substring(0, 63);
        if (uiContext != null)
            uiContext.Post(_ => { try { trayIcon.Text = text; } catch { } }, null);
    }

    void PresenceLoop()
    {
        while (running)
        {
            string name = ProfileContactName();
            var servers = RoomServers();
            if (name == null || servers.Count == 0)
            {
                SetTrayText(READY_TEXT);
                Thread.Sleep(30000);
                continue;
            }
            foreach (var server in servers)
            {
                if (!running)
                    return;
                try { SignedIn(server.Key, server.Value, name); } catch { }
                SetTrayText(READY_TEXT);
                if (ProfileContactName() != name)
                    break;   // profile changed: sign in again under the new name
            }
            Thread.Sleep(5000);
        }
    }

    static void SendLine(Socket s, string line)
    {
        s.Send(Encoding.UTF8.GetBytes(line + "\n"));
    }

    // Returns when the connection ends (relay restarted, network change,
    // profile renamed); PresenceLoop then signs in again.
    void SignedIn(string host, int port, string name)
    {
        using (var client = new TcpClient())
        {
            IAsyncResult ar = client.BeginConnect(host, port, null, null);
            if (!ar.AsyncWaitHandle.WaitOne(3000))
                return;
            client.EndConnect(ar);
            Socket s = client.Client;
            SendLine(s, "PRESENCE " + name);

            var pending = new List<byte>();
            var buf = new byte[1024];
            bool signedIn = false;
            DateTime lastHeard = DateTime.Now, lastPing = DateTime.Now, lastProfileCheck = DateTime.Now;

            while (running)
            {
                // Poll, not a receive timeout: after a timed-out blocking
                // receive Winsock leaves the socket in an undefined state.
                if (s.Poll(1000000, SelectMode.SelectRead))
                {
                    int n = s.Receive(buf);
                    if (n <= 0)
                        return;
                    lastHeard = DateTime.Now;
                    for (int i = 0; i < n; i++)
                    {
                        if (buf[i] != (byte)'\n')
                        {
                            if (buf[i] != (byte)'\r') pending.Add(buf[i]);
                            continue;
                        }
                        string line = Encoding.UTF8.GetString(pending.ToArray());
                        pending.Clear();
                        if (!signedIn)
                        {
                            if (line != "OK")
                                return;
                            signedIn = true;
                            SetTrayText("PMF.RV Notify -- online as " + name);
                        }
                        else if (line.StartsWith("NOTIFY "))
                        {
                            PostNotification(line.Substring(7));
                        }
                    }
                    if (pending.Count > 4096)
                        return;
                }

                DateTime now = DateTime.Now;
                if (!signedIn && (now - lastHeard).TotalSeconds > 5)
                    return;
                if ((now - lastPing).TotalSeconds >= 20)
                {
                    SendLine(s, "PING");
                    lastPing = now;
                }
                if ((now - lastHeard).TotalSeconds > 65)
                    return;   // no PONG for three pings: the server is gone
                if ((now - lastProfileCheck).TotalSeconds >= 10)
                {
                    lastProfileCheck = now;
                    if (ProfileContactName() != name)
                    {
                        try { SendLine(s, "BYE"); } catch { }
                        return;
                    }
                }
            }
        }
    }

    void StartListening()
    {
        listenerThread = new Thread(ListenLoop);
        listenerThread.IsBackground = true;
        listenerThread.Start();
    }

    void ListenLoop()
    {
        try
        {
            listener = new TcpListener(IPAddress.Any, NOTIFY_PORT);
            listener.Start();

            while (running)
            {
                TcpClient client;
                try
                {
                    client = listener.AcceptTcpClient();
                }
                catch
                {
                    break; // listener stopped (Exit was clicked)
                }

                try
                {
                    using (client)
                    using (NetworkStream stream = client.GetStream())
                    {
                        stream.ReadTimeout = 3000;
                        byte[] buffer = new byte[512];
                        int n = stream.Read(buffer, 0, buffer.Length);
                        if (n > 0)
                        {
                            string msg = Encoding.UTF8.GetString(buffer, 0, n).Trim();
                            // Protocol: "PING:<senderName>" -- deliberately
                            // tiny; this app only ever needs to say
                            // "someone wants you," not carry real data.
                            if (msg.StartsWith("PING:"))
                            {
                                string sender = msg.Substring(5);
                                PostNotification(sender);
                            }
                        }
                    }
                }
                catch
                {
                    // one bad/slow connection should never kill the listener
                }
            }
        }
        catch
        {
            // Most likely cause: port already in use (e.g. a second
            // instance, or the port collides with something else).
            // Failing silently is correct for a background service --
            // there is no user at a keyboard to show an error dialog to.
        }
    }

    void PostNotification(string sender)
    {
        SendNotificationCallback showIt = () =>
        {
            trayIcon.BalloonTipTitle = "PMF.RV";
            Match invite = InviteRe.Match(sender);
            if (invite.Success)
            {
                pendingFrom = invite.Groups[1].Value.Trim();
                pendingRoom = invite.Groups[2].Value;
                pendingAt = DateTime.Now;
                trayIcon.BalloonTipText = pendingFrom + " invites you to a review session. Click to join their room.";
            }
            else
            {
                pendingRoom = null;
                trayIcon.BalloonTipText = sender + " wants to start a review session. Click to open RV.";
            }
            trayIcon.ShowBalloonTip(8000);
        };

        if (uiContext != null)
        {
            uiContext.Post(_ => showIt(), null);
        }
        else
        {
            // Should not happen now that uiContext is captured after the
            // tray icon's handle exists, but never silently drop a
            // notification if it somehow does.
            showIt();
        }
    }

    delegate void SendNotificationCallback();

    static string ProfileDir()
    {
        string dir = Environment.GetEnvironmentVariable("PMF_RV_PROFILE_DIR");
        return string.IsNullOrEmpty(dir)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "PMF.RV")
            : dir;
    }

    // Picked up by PMF.RV's Room plugin (pmf_room_mode._check_join_request),
    // whether PMF.RV is already open or is about to start.
    static void WriteJoinRequest(string room, string from)
    {
        string dir = ProfileDir();
        Directory.CreateDirectory(dir);
        string path = Path.Combine(dir, "join_request.json");
        string tmp = path + ".tmp";
        var data = new Dictionary<string, object>
        {
            { "code", room },
            { "from", from },
            { "time", (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds },
        };
        File.WriteAllText(tmp, new JavaScriptSerializer().Serialize(data), new UTF8Encoding(false));
        if (File.Exists(path))
            File.Delete(path);
        File.Move(tmp, path);
    }

    // PMF.RV from THIS install that is already running, if any -- clicking
    // used to start a second copy.
    Process RunningRv()
    {
        string prefix = Path.Combine(selfDir, "app") + "\\";
        foreach (Process p in Process.GetProcessesByName("rv"))
        {
            try
            {
                if (p.MainModule.FileName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
                    && p.MainWindowHandle != IntPtr.Zero)
                    return p;
            }
            catch { }
        }
        return null;
    }

    void LaunchRV()
    {
        try
        {
            string room = pendingRoom != null && (DateTime.Now - pendingAt).TotalMinutes < 10 ? pendingRoom : null;
            string from = pendingFrom;
            pendingRoom = null;
            if (room != null)
                WriteJoinRequest(room, from);

            Process rv = RunningRv();
            if (rv != null)
            {
                if (IsIconic(rv.MainWindowHandle))
                    ShowWindow(rv.MainWindowHandle, 9);   // SW_RESTORE
                SetForegroundWindow(rv.MainWindowHandle);
                return;
            }

            if (!File.Exists(rvToolPath))
            {
                LogLaunchFailure("RVTool.exe not found at: " + rvToolPath);
                return;
            }
            var psi = new ProcessStartInfo(rvToolPath)
            {
                WorkingDirectory = Path.GetDirectoryName(rvToolPath),
                UseShellExecute = true,
            };
            Process.Start(psi);
        }
        catch (Exception e)
        {
            LogLaunchFailure(e.ToString());
        }
    }

    void LogLaunchFailure(string message)
    {
        try
        {
            string self = Path.GetDirectoryName(Application.ExecutablePath);
            string logPath = Path.Combine(self, "notify_launch_log.txt");
            File.AppendAllText(logPath,
                DateTime.Now + "  LaunchRV failed: " + message + Environment.NewLine);
        }
        catch { /* logging itself must never throw */ }
    }

    void ExitApp()
    {
        running = false;
        try { listener.Stop(); } catch { }
        trayIcon.Visible = false;
        Application.Exit();
    }
}
