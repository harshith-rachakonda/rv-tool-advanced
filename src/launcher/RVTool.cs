using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Microsoft.Win32;

// RVTool launcher — no external DLL dependencies.
// Sets up PATH and env vars then launches app\bin\rv.exe.
//
// Build: Source\build.ps1 (dev folder only; Source\ is never published).
[assembly: System.Reflection.AssemblyTitle("PMF.RV")]
[assembly: System.Reflection.AssemblyDescription("PMF.RV Launcher")]
[assembly: System.Reflection.AssemblyProduct("PMF.RV")]
[assembly: System.Reflection.AssemblyVersion("10.6.0.0")] // 10.06

class RVTool
{
    // ------------------------------------------------------------------
    // LAN auto-update -- where updates come from.
    //
    // The list of update locations ships INSIDE the install, in
    // app\etc\pmf_studio.ini ([update] sources), so it travels with every
    // update like any other file. Moving the release share is then: put
    // the new location first in that file, publish once from the OLD
    // location (everyone picks it up on next launch), then move.
    //
    // pmf.9/pmf.10 instead compiled one address into this exe and wrote
    // it into a sibling file (PMF.RVTOOL_update_source.txt) on first
    // launch, freezing it per machine. That sibling file is now ONLY a
    // deliberate manual override; copies that just contain the old
    // auto-written default are removed (CleanupLegacySourceFile).
    //
    // Used only if pmf_studio.ini is missing (damaged install).
    // ------------------------------------------------------------------
    const string BUILTIN_UPDATE_SOURCES = @"\\192.0.2.10\PMF.RVTOOL_release, \\REVIEW-SERVER\PMF.RVTOOL_release";

    // Exactly what pmf.9/pmf.10 auto-wrote into the sibling file.
    const string LEGACY_AUTO_SOURCE = @"\\192.0.2.10\PMF.RVTOOL_release";

    const string OVERRIDE_FILE_NAME = "PMF.RVTOOL_update_source.txt";

    // Sibling marker (deliberately NOT inside the PMF.RVTOOL folder, so
    // publishing never ships it) that turns auto-update OFF on this
    // machine. Used on the dev machine: the dev folder is the publish
    // SOURCE and must never pull from the release copy.
    const string DISABLE_MARKER_NAME = "PMF.RVTOOL_disable_autoupdate.txt";

    // Test hooks (see Source\README_DEV.txt). PMF_RVTOOL_TEST=1: do every
    // pre-launch step but don't start rv.exe and don't show message boxes.
    static readonly bool TestMode = Environment.GetEnvironmentVariable("PMF_RVTOOL_TEST") == "1";

    static List<string> SplitList(string value)
    {
        var list = new List<string>();
        foreach (string part in (value ?? "").Split(','))
        {
            string t = part.Trim();
            if (t.Length > 0)
                list.Add(t);
        }
        return list;
    }

    // Where this install would update from: a manual override if someone
    // deliberately created one, else the shipped config, else built-in.
    // No side effects -- also used by the release-master check below.
    static List<string> ConfiguredSources(string self)
    {
        string overrideFile = Path.Combine(Path.GetDirectoryName(self), OVERRIDE_FILE_NAME);
        try
        {
            if (File.Exists(overrideFile))
            {
                string manual = File.ReadAllText(overrideFile).Trim();
                if (manual.Length > 0 && !SamePath(manual, LEGACY_AUTO_SOURCE))
                    return new List<string> { manual };
            }
        }
        catch { }

        var fromIni = SplitList(ReadIniValue(Path.Combine(self, "app", "etc", "pmf_studio.ini"), "update", "sources", ""));
        return fromIni.Count > 0 ? fromIni : SplitList(BUILTIN_UPDATE_SOURCES);
    }

    static bool AutoUpdateDisabled(string self)
    {
        return File.Exists(Path.Combine(Path.GetDirectoryName(self), DISABLE_MARKER_NAME));
    }

    static void CleanupLegacySourceFile(string self)
    {
        string overrideFile = Path.Combine(Path.GetDirectoryName(self), OVERRIDE_FILE_NAME);
        try
        {
            if (File.Exists(overrideFile) && SamePath(File.ReadAllText(overrideFile).Trim(), LEGACY_AUTO_SOURCE))
            {
                File.Delete(overrideFile);
                LogUpdate(self, "Removed auto-written " + overrideFile + " -- update locations now come from app\\etc\\pmf_studio.ini.");
            }
        }
        catch (Exception ex)
        {
            LogUpdate(self, "Could not remove legacy " + overrideFile + ": " + ex.Message);
        }
    }

    static bool SamePath(string a, string b)
    {
        return string.Equals(a.Trim().TrimEnd('\\'), b.Trim().TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
    }

    static string UncHost(string path)
    {
        if (!path.StartsWith(@"\\"))
            return null;
        int slash = path.IndexOf('\\', 2);
        return slash > 2 ? path.Substring(2, slash - 2) : path.Substring(2);
    }

    static string UncShare(string path)
    {
        if (!path.StartsWith(@"\\"))
            return null;
        string[] parts = path.Substring(2).Split(new[] { '\\' }, StringSplitOptions.RemoveEmptyEntries);
        return parts.Length >= 2 ? parts[1] : null;
    }

    // An unreachable UNC host makes Directory.Exists hang ~20 s on SMB
    // timeouts -- every launch, for anyone off the studio network (the
    // director at home). A 1.5 s TCP probe of port 445 first keeps launch
    // fast; the next source in the list is then tried.
    static bool HostAnswersSmb(string host)
    {
        try
        {
            using (var c = new TcpClient())
            {
                IAsyncResult ar = c.BeginConnect(host, 445, null, null);
                if (!ar.AsyncWaitHandle.WaitOne(1500))
                    return false;
                c.EndConnect(ar);
                return true;
            }
        }
        catch
        {
            return false;
        }
    }

    static string PickReachableSource(string self, List<string> sources)
    {
        var noLogin = new List<string>();
        foreach (string source in sources)
        {
            try
            {
                string host = UncHost(source);
                if (host != null && !HostAnswersSmb(host))
                    continue;
                if (File.Exists(Path.Combine(source, "VERSION.txt")))
                    return source;
                if (host != null)
                    noLogin.Add(source);   // the PC answers, but the share won't open
            }
            catch { }
        }
        if (noLogin.Count > 0)
            LogUpdate(self, "Can reach the update PC but can't open the share (" + string.Join(", ", noLogin.ToArray())
                + ") -- the share login isn't saved on this PC. Press Windows+R, open " + noLogin[0]
                + ", sign in with the reviewshare login and tick 'Remember my credentials'. Starting the installed version.");
        else
            LogUpdate(self, "No update location reachable (" + string.Join(", ", sources.ToArray()) + "); starting the installed version.");
        return null;
    }

    // ------------------------------------------------------------------
    // Refuse to run from the master release copy.
    //
    // Running RV from the folder everyone updates from writes logs and
    // caches into it, starts PMFNotify from it (and points this user's
    // auto-start at it), and locks its files so the next publish fails.
    // Detected as: this folder is network-shared under the same share
    // name as one of the update locations, and that location's host is
    // this machine. (A marker file inside the release can't be used --
    // every client would mirror it.)
    // ------------------------------------------------------------------
    static string MasterShareOf(string self, List<string> sources)
    {
        try
        {
            string localShare = null;
            using (var key = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\LanmanServer\Shares"))
            {
                if (key == null)
                    return null;
                foreach (string name in key.GetValueNames())
                {
                    var values = key.GetValue(name) as string[];
                    if (values == null)
                        continue;
                    foreach (string v in values)
                        if (v.StartsWith("Path=", StringComparison.OrdinalIgnoreCase) && SamePath(v.Substring(5), self))
                            localShare = name;
                }
            }

            foreach (string source in sources)
            {
                if (!source.StartsWith(@"\\"))
                {
                    if (SamePath(source, self))
                        return source;
                    continue;
                }
                if (localShare != null
                    && string.Equals(UncShare(source), localShare, StringComparison.OrdinalIgnoreCase)
                    && IsThisMachine(UncHost(source)))
                    return source;
            }
        }
        catch { }
        return null;
    }

    static bool IsThisMachine(string host)
    {
        if (string.IsNullOrEmpty(host))
            return false;
        if (string.Equals(host, Environment.MachineName, StringComparison.OrdinalIgnoreCase)
            || string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase))
            return true;
        IPAddress ip;
        if (!IPAddress.TryParse(host, out ip))
            return false;
        if (IPAddress.IsLoopback(ip))
            return true;
        foreach (NetworkInterface nic in NetworkInterface.GetAllNetworkInterfaces())
            foreach (UnicastIPAddressInformation a in nic.GetIPProperties().UnicastAddresses)
                if (a.Address.Equals(ip))
                    return true;
        return false;
    }

    // ------------------------------------------------------------------
    // The update itself.
    //
    // Fast path (10.06+): the release carries release_manifest.txt -- every
    // file's size and timestamp, written by publish_release.ps1. The
    // launcher reads that ONE file, compares it with its own folder (local,
    // fast) and copies only what changed. robocopy /MIR instead asks the
    // share about all 10,000+ files one by one: fine on the office LAN, but
    // over Tailscale each question is a round trip and a small update took
    // many minutes -- with nothing on screen, so people clicked RVTool.exe
    // again and again and several updates ran over each other (a remote reviewer,
    // 2026-10-15). Releases without a manifest still use robocopy.
    //
    // While updating:
    //   - a small "Updating PMF.RV" window with progress is shown;
    //   - only one update per install runs at a time (named mutex); a second
    //     launch says so and exits -- the first opens PMF.RV when done;
    //   - each file is copied to *.pmfpart and then moved into place, a file
    //     that is in use (this running launcher, the always-on PMFNotify) is
    //     renamed aside to *.old first -- Windows allows renaming a running
    //     exe but not overwriting it;
    //   - VERSION.txt is stamped LAST and only after every file in the
    //     manifest is verified present with the right size, so an update
    //     that is cut short (closed, killed, network dropped) is simply
    //     finished on the next launch.
    // ------------------------------------------------------------------
    const string MANIFEST_NAME = "release_manifest.txt";

    // Never compared, copied over or deleted at the install root.
    static readonly string[] ROOT_KEEP = { "VERSION.txt", "update_log.txt", "firewall_log.txt", "notify_launch_log.txt", MANIFEST_NAME };

    static bool IsKept(string rel)
    {
        string ext = Path.GetExtension(rel).ToLowerInvariant();
        if (ext == ".old" || ext == ".new" || ext == ".pmfpart")
            return true;
        if (rel.IndexOf('\\') < 0)
            foreach (string k in ROOT_KEEP)
                if (string.Equals(rel, k, StringComparison.OrdinalIgnoreCase))
                    return true;
        return false;
    }

    class ManifestFile
    {
        public string Rel;
        public long Size;
        public long Ticks;
    }

    // "F|size|lastWriteUtcTicks|relative\\path" and "D|0|0|relative\\dir"
    static bool ReadManifest(string path, List<ManifestFile> files, List<string> dirs)
    {
        if (!File.Exists(path))
            return false;
        foreach (string line in File.ReadAllLines(path, Encoding.UTF8))
        {
            if (line.Length == 0 || line[0] == '#')
                continue;
            string[] f = line.Split(new[] { '|' }, 4);
            if (f.Length != 4 || f[3].Length == 0 || f[3].Contains("..") || Path.IsPathRooted(f[3]))
                throw new InvalidDataException("bad manifest line: " + line);
            if (f[0] == "D")
                dirs.Add(f[3]);
            else if (f[0] == "F")
                files.Add(new ManifestFile { Rel = f[3], Size = long.Parse(f[1]), Ticks = long.Parse(f[2]) });
        }
        return files.Count > 0;
    }

    // A list whose paths don't line up with the release (written for another
    // folder, truncated, corrupted) must never drive an update: it would make
    // every local file look "not in the release". The core files must be
    // listed AND really be on the share.
    static bool ManifestLooksRight(string source, List<ManifestFile> files)
    {
        string[] core = { "RVTool.exe", @"app\bin\rv.exe" };
        foreach (string c in core)
        {
            ManifestFile m = files.Find(f => string.Equals(f.Rel, c, StringComparison.OrdinalIgnoreCase));
            if (m == null)
                return false;
            var fi = new FileInfo(Path.Combine(source, c));
            if (!fi.Exists || fi.Length != m.Size)
                return false;
        }
        return true;
    }

    static bool SameFile(FileInfo fi, ManifestFile m)
    {
        return fi.Exists && fi.Length == m.Size
            && Math.Abs(fi.LastWriteTimeUtc.Ticks - m.Ticks) <= TimeSpan.TicksPerSecond * 2;
    }

    // Returns true when the folder now matches the manifest exactly.
    static bool ManifestUpdate(string self, string source, List<ManifestFile> files, List<string> dirs,
                               UpdateWindow ui, out bool notifyReplaced)
    {
        notifyReplaced = false;

        // Leftovers of an earlier, interrupted run.
        foreach (string part in Directory.GetFiles(self, "*.pmfpart", SearchOption.AllDirectories))
            try { File.Delete(part); } catch { }

        foreach (string d in dirs)
            Directory.CreateDirectory(Path.Combine(self, d));

        var todo = new List<ManifestFile>();
        long bytesTotal = 0;
        foreach (ManifestFile m in files)
        {
            if (IsKept(m.Rel))
                continue;
            if (!SameFile(new FileInfo(Path.Combine(self, m.Rel)), m))
            {
                todo.Add(m);
                bytesTotal += m.Size;
            }
        }
        LogUpdate(self, todo.Count + " of " + files.Count + " files changed (" + (bytesTotal / (1024 * 1024)) + " MB).");

        int failed = 0, done = 0;
        long bytesDone = 0;
        foreach (ManifestFile m in todo)
        {
            done++;
            if (ui != null)
                ui.Set("Updating PMF.RV -- file " + done + " of " + todo.Count + "\n" + Path.GetFileName(m.Rel),
                       bytesTotal > 0 ? (int)(bytesDone * 100 / bytesTotal) : 0);
            string dst = Path.Combine(self, m.Rel);
            string tmp = dst + ".pmfpart";
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(dst));
                File.Copy(Path.Combine(source, m.Rel), tmp, true);
                File.SetLastWriteTimeUtc(tmp, new DateTime(m.Ticks, DateTimeKind.Utc));
                if (File.Exists(dst))
                {
                    try { File.Delete(dst); }
                    catch { File.Move(dst, dst + "." + DateTime.Now.Ticks + ".old"); }   // in use
                }
                File.Move(tmp, dst);
                if (string.Equals(m.Rel, "PMFNotify.exe", StringComparison.OrdinalIgnoreCase))
                    notifyReplaced = true;
            }
            catch (Exception ex)
            {
                failed++;
                LogUpdate(self, "Could not update " + m.Rel + ": " + ex.Message);
                try { File.Delete(tmp); } catch { }
            }
            bytesDone += m.Size;
        }

        if (failed > 0)
        {
            LogUpdate(self, "Update incomplete (" + failed + " file(s) could not be copied) -- nothing removed, version NOT advanced; it continues on the next launch.");
            return false;
        }

        // Mirror: remove what the release no longer has (as robocopy /MIR did)
        // -- only after every copy succeeded, and never a large share of the
        // install: that would mean the list is wrong, not that the release
        // really dropped those files.
        var want = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (ManifestFile m in files)
            want.Add(m.Rel);
        string[] localFiles = Directory.GetFiles(self, "*", SearchOption.AllDirectories);
        var extras = new List<string>();
        foreach (string local in localFiles)
        {
            string rel = local.Substring(self.Length + 1);
            if (!IsKept(rel) && !want.Contains(rel))
                extras.Add(local);
        }
        if (extras.Count > Math.Max(50, localFiles.Length / 4))
        {
            LogUpdate(self, "Refusing to remove " + extras.Count + " of " + localFiles.Length
                + " files -- the release file list looks wrong. Nothing removed, version NOT advanced.");
            return false;
        }
        foreach (string local in extras)
        {
            try { File.Delete(local); }
            catch (Exception ex) { LogUpdate(self, "Could not remove " + local.Substring(self.Length + 1) + ": " + ex.Message); }
        }
        var wantDirs = new HashSet<string>(dirs, StringComparer.OrdinalIgnoreCase);
        var localDirs = new List<string>(Directory.GetDirectories(self, "*", SearchOption.AllDirectories));
        localDirs.Sort((a, b) => b.Length.CompareTo(a.Length));   // deepest first
        foreach (string d in localDirs)
        {
            string rel = d.Substring(self.Length + 1);
            if (wantDirs.Contains(rel))
                continue;
            try { if (Directory.GetFileSystemEntries(d).Length == 0) Directory.Delete(d); } catch { }
        }

        // Verify before anything claims to be the new version.
        int missing = 0;
        foreach (ManifestFile m in files)
            if (!IsKept(m.Rel) && !SameFile(new FileInfo(Path.Combine(self, m.Rel)), m))
                missing++;
        if (missing > 0 || failed > 0)
        {
            LogUpdate(self, "Update incomplete (" + failed + " failed, " + missing + " not matching) -- version NOT advanced; it continues on the next launch.");
            return false;
        }
        return true;
    }

    static string UpdateMutexName(string self)
    {
        using (var sha = SHA1.Create())
        {
            byte[] h = sha.ComputeHash(Encoding.UTF8.GetBytes(self.ToLowerInvariant()));
            return "PMF_RV_Update_" + BitConverter.ToString(h, 0, 8).Replace("-", "");
        }
    }

    // Small always-on-top window shown only while an update runs. Lives on
    // its own STA thread so the copy loop never has to pump messages.
    class UpdateWindow
    {
        System.Windows.Forms.Form form;
        System.Windows.Forms.Label label;
        System.Windows.Forms.ProgressBar bar;
        readonly ManualResetEvent ready = new ManualResetEvent(false);

        public UpdateWindow(string text)
        {
            var t = new Thread(() =>
            {
                form = new System.Windows.Forms.Form
                {
                    Text = "PMF.RV",
                    FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedDialog,
                    StartPosition = System.Windows.Forms.FormStartPosition.CenterScreen,
                    ClientSize = new System.Drawing.Size(420, 96),
                    ControlBox = false,
                    TopMost = true,
                };
                try { form.Icon = System.Drawing.Icon.ExtractAssociatedIcon(System.Windows.Forms.Application.ExecutablePath); } catch { }
                label = new System.Windows.Forms.Label { Left = 16, Top = 12, Width = 388, Height = 40, Text = text };
                bar = new System.Windows.Forms.ProgressBar
                {
                    Left = 16, Top = 60, Width = 388, Height = 20,
                    Style = System.Windows.Forms.ProgressBarStyle.Marquee,
                };
                form.Controls.Add(label);
                form.Controls.Add(bar);
                form.Shown += (s, e) => ready.Set();
                System.Windows.Forms.Application.Run(form);
            });
            t.SetApartmentState(ApartmentState.STA);
            t.IsBackground = true;
            t.Start();
            ready.WaitOne(5000);
        }

        public void Set(string text, int percent)
        {
            try
            {
                form.BeginInvoke((Action)(() =>
                {
                    label.Text = text;
                    if (percent >= 0)
                    {
                        bar.Style = System.Windows.Forms.ProgressBarStyle.Blocks;
                        bar.Value = Math.Max(0, Math.Min(100, percent));
                    }
                }));
            }
            catch { }
        }

        public void Close()
        {
            try { form.BeginInvoke((Action)(() => form.Close())); } catch { }
        }
    }

    // Returns false when another launch of this install is already updating:
    // the caller exits, and that other launch opens PMF.RV when it is done.
    static bool TryAutoUpdate(string self, string source)
    {
        var movedAside = new List<KeyValuePair<string, string>>();
        Mutex gate = null;
        UpdateWindow ui = null;
        try
        {
            string localVersionFile  = Path.Combine(self, "VERSION.txt");
            string remoteVersionFile = Path.Combine(source, "VERSION.txt");

            // Read once, up front: if a publish lands while this client is
            // updating, what gets stamped below must describe the files
            // copied, not the newer ones.
            byte[] remoteVersionBytes = File.ReadAllBytes(remoteVersionFile);
            string localVersion  = ReadVersionString(localVersionFile);
            string remoteVersion = ReadVersionString(remoteVersionFile);

            if (localVersion == remoteVersion)
                return true; // already current

            gate = new Mutex(false, UpdateMutexName(self));
            bool mine;
            try { mine = gate.WaitOne(0); }
            catch (AbandonedMutexException) { mine = true; }   // a killed update: carry on
            if (!mine)
            {
                gate.Dispose();
                gate = null;
                LogUpdate(self, "Another launch is already updating this install; not starting a second update.");
                ShowInfo("PMF.RV is updating",
                    "PMF.RV is already being updated (" + localVersion + " -> " + remoteVersion + ").\n\n"
                    + "It opens by itself when the update is finished -- no need to start it again.");
                return false;
            }

            // rv.exe and its DLLs can't be replaced while it runs, and
            // yanking files out from under a live session would be worse
            // than updating on the next launch.
            if (RvRunningFrom(self))
            {
                LogUpdate(self, "Update available (" + localVersion + " -> " + remoteVersion + ") but PMF.RV is still running; skipping this launch.");
                return true;
            }

            LogUpdate(self, "Updating " + localVersion + " -> " + remoteVersion + " from " + source + " ...");
            if (!TestMode || Environment.GetEnvironmentVariable("PMF_RVTOOL_TEST_UI") == "1")
                ui = new UpdateWindow("Updating PMF.RV to " + remoteVersion + " -- please wait.\nPMF.RV opens by itself when it's done.");

            List<int> notifyPids = ProcessIdsRunning(Path.Combine(self, "PMFNotify.exe"));
            bool notifyReplaced;

            var mfiles = new List<ManifestFile>();
            var mdirs = new List<string>();
            bool haveManifest = false;
            try { haveManifest = ReadManifest(Path.Combine(source, MANIFEST_NAME), mfiles, mdirs); }
            catch (Exception ex) { LogUpdate(self, "Release file list unreadable (" + ex.Message + "); using a full comparison."); }

            if (haveManifest && !ManifestLooksRight(source, mfiles))
            {
                LogUpdate(self, "Release file list doesn't match the release folder; using a full comparison instead.");
                haveManifest = false;
            }

            if (haveManifest)
            {
                if (!ManifestUpdate(self, source, mfiles, mdirs, ui, out notifyReplaced))
                    return true;
            }
            else
            {
                foreach (string exe in Directory.GetFiles(self, "*.exe"))
                {
                    var mine2 = new FileInfo(exe);
                    var theirs = new FileInfo(Path.Combine(source, mine2.Name));
                    if (theirs.Exists && theirs.Length == mine2.Length && theirs.LastWriteTimeUtc == mine2.LastWriteTimeUtc)
                        continue; // robocopy would skip it anyway
                    string aside = exe + "." + DateTime.Now.Ticks + ".old";
                    try
                    {
                        File.Move(exe, aside);
                        movedAside.Add(new KeyValuePair<string, string>(exe, aside));
                    }
                    catch (Exception ex)
                    {
                        LogUpdate(self, "Could not move " + mine2.Name + " aside: " + ex.Message);
                    }
                }

                var psi = new ProcessStartInfo
                {
                    FileName        = "robocopy.exe",
                    // /MIR mirrors exactly. Exit codes 0-7 are success; 8+ failure.
                    // VERSION.txt is stamped LAST, below; *.old / *.new /
                    // *.pmfpart may be running or in progress, never purged.
                    Arguments       = "\"" + source + "\" \"" + self + "\" /MIR /R:1 /W:1 /NFL /NDL /NP /NJH /XF update_log.txt VERSION.txt "
                                      + MANIFEST_NAME + " *.old *.new *.pmfpart",
                    UseShellExecute = false,
                    CreateNoWindow  = true,
                };

                using (var p = Process.Start(psi))
                {
                    p.WaitForExit();
                    // A robocopy ended from Task Manager exits with 1, which
                    // looks like success -- only 0-7 from a normal run are.
                    if (p.ExitCode >= 8)
                    {
                        LogUpdate(self, "robocopy finished, exit code " + p.ExitCode
                            + "  (>=8 = FAILURE) -- version NOT advanced; will retry next launch.");
                        RestoreMovedAside(self, movedAside);
                        return true;
                    }
                }
                notifyReplaced = movedAside.Exists(m => Path.GetFileName(m.Key).Equals("PMFNotify.exe", StringComparison.OrdinalIgnoreCase));
            }

            byte[] after = File.ReadAllBytes(remoteVersionFile);
            if (!BytesEqual(after, remoteVersionBytes))
            {
                LogUpdate(self, "A new release was published during this update -- not stamping; it finishes on the next launch.");
                return true;
            }
            File.WriteAllBytes(localVersionFile, remoteVersionBytes);
            LogUpdate(self, "Update finished; now at " + remoteVersion);

            if (notifyReplaced)
            {
                foreach (int pid in notifyPids)
                {
                    try { Process.GetProcessById(pid).Kill(); } catch { }
                }
                // EnsureNotifyRunning (called right after) starts the new one.
            }
            return true;
        }
        catch (Exception ex)
        {
            LogUpdate(self, "Auto-update failed, continuing with existing install: " + ex.Message);
            RestoreMovedAside(self, movedAside);
            return true;
        }
        finally
        {
            if (ui != null)
                ui.Close();
            if (gate != null)
            {
                try { gate.ReleaseMutex(); } catch { }
                gate.Dispose();
            }
        }
    }

    static void RestoreMovedAside(string self, List<KeyValuePair<string, string>> movedAside)
    {
        foreach (var m in movedAside)
        {
            try
            {
                if (!File.Exists(m.Key))
                    File.Move(m.Value, m.Key);
            }
            catch (Exception ex)
            {
                LogUpdate(self, "Could not restore " + m.Key + ": " + ex.Message);
            }
        }
    }

    static bool BytesEqual(byte[] a, byte[] b)
    {
        if (a.Length != b.Length)
            return false;
        for (int i = 0; i < a.Length; i++)
            if (a[i] != b[i])
                return false;
        return true;
    }

    // Leftovers from earlier updates; ones still running stay locked and
    // are simply retried on a later launch.
    static void CleanupMovedAside(string self)
    {
        foreach (string pattern in new[] { "*.old", "*.new" })
        {
            foreach (string f in Directory.GetFiles(self, pattern))
            {
                try { File.Delete(f); } catch { }
            }
        }
    }

    static List<int> ProcessIdsRunning(string exePath)
    {
        var pids = new List<int>();
        string name = Path.GetFileNameWithoutExtension(exePath);
        foreach (Process p in Process.GetProcessesByName(name))
        {
            try
            {
                if (SamePath(p.MainModule.FileName, exePath))
                    pids.Add(p.Id);
            }
            catch { }
        }
        return pids;
    }

    // Only rv.exe from THIS install locks these files -- another install's
    // RV (e.g. on the dev machine) must not block the update. If a
    // process's path can't be read, assume it's ours to be safe.
    static bool RvRunningFrom(string self)
    {
        string prefix = self.TrimEnd('\\') + "\\";
        foreach (Process p in Process.GetProcessesByName("rv"))
        {
            try
            {
                if (p.MainModule.FileName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            catch
            {
                return true;
            }
        }
        return false;
    }

    [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern long WritePrivateProfileString(string section, string key, string val, string filePath);

    [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern int GetPrivateProfileString(string section, string key, string defaultValue, StringBuilder retVal, int size, string filePath);

    static string ReadIniValue(string filePath, string section, string key, string defaultValue)
    {
        if (!File.Exists(filePath))
            return defaultValue;
        var sb = new StringBuilder(4096);
        GetPrivateProfileString(section, key, defaultValue, sb, sb.Capacity, filePath);
        return sb.ToString();
    }

    // ------------------------------------------------------------------
    // Studio-settings migration.
    //
    // RV_PREFS_OVERRIDE_PATH (below) only fills in settings an artist's
    // live file DOESN'T already have -- it never overwrites an existing
    // value, so a machine with ANY prior RV/OpenRV install keeps old
    // values for keys like networkOnStartup forever.
    //
    // app\etc\OpenRV_migrate.ini lists the specific Section/Key pairs that
    // are FORCED (studio-wide settings only -- never personal ones like
    // contacts, window layout, or audio device), written key-by-key with
    // WritePrivateProfileString.
    //
    // It runs once per distinct set of forced settings: a hash of the
    // effective key=value lines is stamped into the live file
    // ([PMF] migratedHash). pmf.6-pmf.10 keyed this on VERSION.txt
    // instead, which re-forced these keys -- undoing artists' own later
    // changes -- on EVERY release, even one that only fixed a plugin.
    // ------------------------------------------------------------------
    static void TryMigrateSettings(string self, string app)
    {
        try
        {
            string migrateFile = Path.Combine(app, "etc", "OpenRV_migrate.ini");
            if (!File.Exists(migrateFile))
                return; // nothing curated to force -- no-op

            var entries = new List<string[]>();
            var canonical = new StringBuilder();
            string section = "";
            foreach (string rawLine in File.ReadAllLines(migrateFile))
            {
                string line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith(";") || line.StartsWith("#"))
                    continue;

                if (line.StartsWith("[") && line.EndsWith("]"))
                {
                    section = line.Substring(1, line.Length - 2);
                    continue;
                }

                int eq = line.IndexOf('=');
                if (eq < 0 || section.Length == 0)
                    continue;

                string key   = line.Substring(0, eq).Trim();
                string value = SanitizeIniValue(line.Substring(eq + 1).Trim());
                entries.Add(new[] { section, key, value });
                canonical.Append("[" + section + "]" + key + "=" + value + "\n");
            }

            string hash;
            using (var sha = SHA256.Create())
                hash = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(canonical.ToString())))
                    .Replace("-", "").Substring(0, 16).ToLowerInvariant();

            string liveIni = Environment.GetEnvironmentVariable("PMF_RVTOOL_TEST_SETTINGS_INI");
            if (!TestMode || string.IsNullOrEmpty(liveIni))
                liveIni = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    "ASWF", "OpenRV.ini");

            if (ReadIniValue(liveIni, "PMF", "migratedHash", "") == hash)
                return; // these exact settings were already forced once -- leave artists' later changes alone

            string liveDir = Path.GetDirectoryName(liveIni);
            if (!Directory.Exists(liveDir))
                Directory.CreateDirectory(liveDir); // fresh machine, RV has never run yet

            foreach (string[] e in entries)
                WritePrivateProfileString(e[0], e[1], e[2], liveIni);

            string version = SanitizeIniValue(ReadVersionString(Path.Combine(self, "VERSION.txt")));
            WritePrivateProfileString("PMF", "migratedHash", hash, liveIni);
            WritePrivateProfileString("PMF", "migratedVersion", version, liveIni); // informational only
            LogUpdate(self, "Settings migration applied (" + entries.Count + " keys, set " + hash + ", " + version + ") -> " + liveIni);
        }
        catch (Exception ex)
        {
            LogUpdate(self, "Settings migration failed: " + ex.Message);
        }
    }

    // A multi-line string handed to WritePrivateProfileString corrupts the
    // INI structure; collapse any line breaks whatever the value's source.
    static string SanitizeIniValue(string value)
    {
        return value.Replace("\r", "").Replace("\n", " ").Trim();
    }

    // VERSION.txt is a decorated multi-line document; extract just the
    // "Internal Version" line's value (fallback: first non-empty line,
    // NEVER the whole file).
    static string ReadVersionString(string versionFilePath)
    {
        if (!File.Exists(versionFilePath))
            return "";

        string[] lines = File.ReadAllLines(versionFilePath);

        foreach (string line in lines)
        {
            int idx = line.IndexOf("Internal Version", StringComparison.OrdinalIgnoreCase);
            if (idx >= 0)
            {
                int colon = line.IndexOf(':', idx);
                if (colon >= 0)
                    return line.Substring(colon + 1).Trim();
            }
        }

        foreach (string line in lines)
        {
            string t = line.Trim();
            if (t.Length > 0)
                return t;
        }

        return "";
    }

    // ------------------------------------------------------------------
    // Auto-start PMF.RV Notify and one-time firewall setup.
    //
    //   - PMFNotify.exe: started if it isn't already up. It self-registers
    //     for auto-start on future logins the first time it runs.
    //   - Firewall rules: READING rules needs no elevation (only changing
    //     them does), so this checks first and only launches
    //     SetupFirewall.exe -- which pops the one UAC prompt itself via
    //     its embedded manifest -- when a rule is missing or points at a
    //     different folder. Launched detached so a slow or ignored UAC
    //     prompt never delays RV opening.
    // ------------------------------------------------------------------
    static void EnsureNotifyRunning(string self)
    {
        try
        {
            if (Process.GetProcessesByName("PMFNotify").Length > 0)
                return; // already running (or auto-started at login)

            string notifyExe = Path.Combine(self, "PMFNotify.exe");
            if (!File.Exists(notifyExe))
                return;

            Process.Start(new ProcessStartInfo
            {
                FileName        = notifyExe,
                WorkingDirectory = self,
                UseShellExecute = true,
            });
        }
        catch (Exception ex)
        {
            LogUpdate(self, "Could not auto-start PMF.RV Notify: " + ex.Message);
        }
    }

    // Every rule SetupFirewall.exe creates, and for program-scoped ones the
    // exe they must point at. A program rule left pointing at a folder the
    // install was moved/re-extracted from doesn't cover the new rv.exe:
    // Windows then asks the artist, and "Cancel" there creates a BLOCK rule
    // that overrides every allow rule -- Sync Review silently dead.
    static string[][] ExpectedFirewallRules(string self)
    {
        return new[]
        {
            new[] { "PMF.RV Sync Review (rv.exe)", Path.Combine(self, "app", "bin", "rv.exe") },
            new[] { "PMF.RV Sync Review (ports 45124-45133)", null },
            new[] { "PMF.RV Notify (PMFNotify.exe)", Path.Combine(self, "PMFNotify.exe") },
            new[] { "PMF.RV Notify (port 45140)", null },
            new[] { "PMF.RV Room Discovery (UDP port 45141)", null },
        };
    }

    // In-process via the firewall's COM API (HNetCfg.FwPolicy2) -- one
    // netsh.exe launch per rule used to add about a second to every launch.
    // Returns the first problem found, or null if everything is in place.
    static string FirewallProblem(string self)
    {
        Type policyType = Type.GetTypeFromProgID("HNetCfg.FwPolicy2");
        object policy = Activator.CreateInstance(policyType);
        object rules = policyType.InvokeMember("Rules", BindingFlags.GetProperty, null, policy, null);
        foreach (string[] expected in ExpectedFirewallRules(self))
        {
            object rule;
            try
            {
                rule = rules.GetType().InvokeMember("Item", BindingFlags.InvokeMethod, null, rules, new object[] { expected[0] });
            }
            catch
            {
                return "missing rule \"" + expected[0] + "\"";
            }
            if (expected[1] != null)
            {
                object app = rule.GetType().InvokeMember("ApplicationName", BindingFlags.GetProperty, null, rule, null);
                if (app == null || !SamePath(Environment.ExpandEnvironmentVariables(app.ToString()), expected[1]))
                    return "rule \"" + expected[0] + "\" points at " + app + ", not this install";
            }
        }
        return null;
    }

    static void EnsureFirewallRules(string self)
    {
        try
        {
            string problem = FirewallProblem(self);
            if (problem == null)
                return;

            string setupExe = Path.Combine(self, "SetupFirewall.exe");
            if (!File.Exists(setupExe))
            {
                LogUpdate(self, "Firewall needs setup (" + problem + ") but SetupFirewall.exe is missing.");
                return;
            }

            LogUpdate(self, "Firewall needs setup (" + problem + ") -- launching SetupFirewall.exe (will prompt for admin).");
            if (TestMode)
                return;
            Process.Start(new ProcessStartInfo
            {
                FileName        = setupExe,
                WorkingDirectory = self,
                UseShellExecute = true,
            });
        }
        catch (Exception ex)
        {
            LogUpdate(self, "Could not check/launch firewall setup: " + ex.Message);
        }
    }

    static void LogUpdate(string self, string message)
    {
        try
        {
            File.AppendAllText(
                Path.Combine(self, "update_log.txt"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + message + Environment.NewLine);
        }
        catch { /* logging is best-effort; never block launch over it */ }
    }

    static void ShowInfo(string title, string text)
    {
        if (TestMode)
        {
            Console.Error.WriteLine(title + ": " + text);
            return;
        }
        System.Windows.Forms.MessageBox.Show(text, title,
            System.Windows.Forms.MessageBoxButtons.OK,
            System.Windows.Forms.MessageBoxIcon.Information);
    }

    static void ShowError(string title, string text)
    {
        if (TestMode)
        {
            Console.Error.WriteLine(title + ": " + text);
            return;
        }
        System.Windows.Forms.MessageBox.Show(text, title,
            System.Windows.Forms.MessageBoxButtons.OK,
            System.Windows.Forms.MessageBoxIcon.Error);
    }

    static int Main(string[] args)
    {
        string self = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string testSelf = Environment.GetEnvironmentVariable("PMF_RVTOOL_TEST_SELF");
        if (TestMode && !string.IsNullOrEmpty(testSelf))
            self = Path.GetFullPath(testSelf).TrimEnd('\\');

        List<string> sources = ConfiguredSources(self);

        // Before anything writes into this folder.
        string master = MasterShareOf(self, sources);
        if (master != null)
        {
            ShowError("PMF.RV – Master release copy",
                "This folder is the master release copy that everyone's PMF.RV updates from:\n\n"
                + "    " + master + "\n\n"
                + "Running PMF.RV from here would write logs and caches into it and lock its files, "
                + "so they would be sent to everyone or the next publish would fail.\n\n"
                + "Run PMF.RV from your own copy instead (for example the dev folder).");
            return 2;
        }

        CleanupMovedAside(self);

        if (!AutoUpdateDisabled(self))
        {
            CleanupLegacySourceFile(self);
            string source = PickReachableSource(self, sources);
            if (source != null)
                if (!TryAutoUpdate(self, source))
                    return 3;   // another launch is updating; it opens PMF.RV
        }

        string app   = Path.Combine(self, "app");

        TryMigrateSettings(self, app);
        EnsureFirewallRules(self);
        EnsureNotifyRunning(self);

        string bin   = Path.Combine(app,  "bin");
        string lib   = Path.Combine(app,  "lib");
        string plug  = Path.Combine(app,  "PlugIns");
        string outpl = Path.Combine(plug, "Output");
        string rv    = Path.Combine(bin,  "rv.exe");

        if (!File.Exists(rv))
        {
            ShowError("PMF.RV – Launch Error",
                "rv.exe not found at:\n" + rv +
                "\n\nEnsure the 'app' folder is present next to RVTool.exe.");
            return 1;
        }

        if (TestMode)
            return 0;

        // Prepend app dirs to PATH so DLLs resolve at rv.exe load time
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        Environment.SetEnvironmentVariable("PATH", bin + ";" + lib + ";" + outpl + ";" + path);

        Environment.SetEnvironmentVariable("RV_HOME",                self + "\\app");
        Environment.SetEnvironmentVariable("RV_PLUGIN_PATH",         plug);
        // RV_PREFS_OVERRIDE_PATH only supplies first-run defaults (cacheMode, vsync,
        // audio device). Once an artist changes a setting in RV, their own saved
        // preference in %APPDATA%\ASWF\OpenRV.ini wins on every later launch.
        // Do NOT set RV_PREFS_CLOBBER_PATH here — that forces this override to win
        // over saved user preferences on every launch, which is what was resetting
        // every artist's settings back to these shipped defaults.
        Environment.SetEnvironmentVariable("RV_PREFS_OVERRIDE_PATH", Path.Combine(app, "etc"));

        Environment.SetEnvironmentVariable("RV_PREF_GLOBAL_PRORES_DECODER_THREADS", "8");
        Environment.SetEnvironmentVariable("RV_MOVIEFFMPEG_ARGS",    "--codecThreads 8");
        Environment.SetEnvironmentVariable("TWK_MIOFFMPEG_IGNORE_ICC_PROFILE", "1");

        // Build args string (pass through any arguments given to RVTool.exe)
        string argStr = args.Length > 0
            ? "\"" + string.Join("\" \"", args) + "\""
            : "";

        var psi = new ProcessStartInfo
        {
            FileName        = rv,
            Arguments       = argStr,
            UseShellExecute = false,
        };

        var proc = Process.Start(psi);
        proc.WaitForExit();
        return proc.ExitCode;
    }
}
