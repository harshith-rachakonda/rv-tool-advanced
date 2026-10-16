using System;
using System.Diagnostics;
using System.IO;

// PMF.RVTOOL - Sync Review + PMF.RV Notify firewall setup
//
// A .exe with an embedded "requireAdministrator" manifest instead of a
// .bat run via the "Run as administrator" verb -- that verb goes through
// a legacy shell code path for .bat elevation that does not handle "&"
// in the folder path correctly (confirmed on this machine: the elevated
// process was dying before running a single command). A manifested .exe
// elevates through the normal, reliable OS mechanism instead.
[assembly: System.Reflection.AssemblyTitle("PMF.RVTOOL Firewall Setup")]
[assembly: System.Reflection.AssemblyProduct("PMF.RVTOOL")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]

class SetupFirewall
{
    static int Main()
    {
        string self = AppDomain.CurrentDomain.BaseDirectory;
        string rvExe = Path.Combine(self, "app", "bin", "rv.exe");
        string notifyExe = Path.Combine(self, "PMFNotify.exe");
        string logPath = Path.Combine(self, "firewall_log.txt");

        var log = new System.Text.StringBuilder();
        Action<string> say = (s) =>
        {
            Console.WriteLine(s);
            log.AppendLine(s);
        };

        say("==== " + DateTime.Now + " ====");
        say("Setting up Windows Firewall rules for PMF.RV Sync Review and PMF.RV Notify...");
        say("");

        AddRule(say, "PMF.RV Sync Review (rv.exe)", "program=\"" + rvExe + "\"", "TCP");
        AddRule(say, "PMF.RV Sync Review (ports 45124-45133)", "localport=45124-45133", "TCP");
        AddRule(say, "PMF.RV Notify (PMFNotify.exe)", "program=\"" + notifyExe + "\"", "TCP");
        AddRule(say, "PMF.RV Notify (port 45140)", "localport=45140", "TCP");
        AddRule(say, "PMF.RV Room Discovery (UDP port 45141)", "localport=45141", "UDP");

        // pmf.10 also added a room-server rule on every PC. The room server
        // now runs on one machine only and creates its own rule when
        // installed there (PMFRoomServer.exe --install), so drop this one.
        RunNetsh("advfirewall firewall delete rule name=\"PMF.RV Room Server (PMFRoomServer.exe)\"", say, quiet: true);

        say("");
        say("Done. Sync Review and PMF.RV Notify connections from machines on");
        say("this same local subnet should no longer be blocked by Windows Firewall.");

        try { File.AppendAllText(logPath, log.ToString()); } catch { }

        Console.WriteLine();
        Console.WriteLine("Press any key to close this window...");
        try { Console.ReadKey(); } catch { }
        return 0;
    }

    static void AddRule(Action<string> say, string name, string matchArg, string protocol,
        string remoteIp = "localsubnet,100.64.0.0/10")
    {
        say("Adding rule: " + name);
        RunNetsh("advfirewall firewall delete rule name=\"" + name + "\"", say, quiet: true);
        // profile includes "public" because Windows classifies plenty of
        // legitimate office/studio networks as Public (not just random
        // cafe WiFi) -- without it, these rules silently don't apply at
        // all on such a network. remoteip stays scoped (not "any"): that's
        // the actual safety boundary (who can reach the port), separate
        // from when the rule is active at all.
        //
        // remoteip includes 100.64.0.0/10 (Tailscale's CGNAT range) on
        // top of localsubnet, for off-LAN reviewers (e.g. a director on
        // set) joined via the studio's Tailscale network. This is NOT the
        // same risk as opening to "any": a packet can only arrive on this
        // machine's Tailscale virtual adapter with a 100.x.x.x address if
        // it already passed through Tailscale's own authenticated,
        // encrypted tunnel -- nobody can reach this adapter by being on
        // the same WiFi/LAN or the open internet. Tailscale's own login
        // is still the actual gatekeeper; this just lets the firewall
        // recognize traffic that already passed that check.
        string addArgs = "advfirewall firewall add rule name=\"" + name + "\" dir=in action=allow "
            + matchArg + " protocol=" + protocol + " enable=yes profile=private,domain,public remoteip=" + remoteIp;
        RunNetsh(addArgs, say, quiet: false);
    }

    static void RunNetsh(string args, Action<string> say, bool quiet)
    {
        try
        {
            var psi = new ProcessStartInfo("netsh", args)
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            using (var p = Process.Start(psi))
            {
                string outText = p.StandardOutput.ReadToEnd();
                string errText = p.StandardError.ReadToEnd();
                p.WaitForExit();
                if (!quiet)
                {
                    if (!string.IsNullOrWhiteSpace(outText)) say("  " + outText.Trim());
                    if (!string.IsNullOrWhiteSpace(errText)) say("  ERROR: " + errText.Trim());
                }
            }
        }
        catch (Exception e)
        {
            if (!quiet) say("  ERROR running netsh: " + e.Message);
        }
    }
}
