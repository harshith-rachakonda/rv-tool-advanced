# PMF.RVTOOL - Publish dev folder to the shared release copy
#
# This is the ONLY way changes in this dev folder should reach
# colleagues. RVTool.exe's auto-update reads VERSION.txt straight from the
# shared release copy, so colleagues only ever see what's actually in
# C:\PMF.RVTOOL_release, never this live dev folder mid-edit.
#
#   Right-click -> Run with PowerShell        publish
#   .\publish_release.ps1 -DryRun             show what would change, touch nothing
#
# Before copying anything it checks, and refuses to publish if:
#   - VERSION.txt's Internal Version is the same as the release's (clients
#     only update when the version CHANGES, so such a publish reaches no one)
#   - README.txt's header doesn't show that same version
#   - a Source\*.cs is newer than its exe (run Source\build.ps1 first)
#   - a PMF plugin has a Python syntax error
#   - PMF.RV or PMF.RV Notify is running FROM the release folder
# and it refreshes each pmf_*.rvpkg from the matching app\PlugIns\Python
# .py so a package can never ship stale code.
#
# VERSION.txt is copied LAST: a colleague whose PMF.RV checks for updates
# while this runs must never see the new version next to half-copied files.
#
# PowerShell, not .bat: "&" in this folder's path (USER_R&D) breaks
# cmd.exe's line parser.

param([switch]$DryRun, [switch]$NoPause)

$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$dest = "C:\PMF.RVTOOL_release"

function Finish([int]$code) {
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit $code
}

function Refuse([string]$why) {
    Write-Host ""
    Write-Host "NOT PUBLISHED: $why" -ForegroundColor Red
    Finish 1
}

function Get-InternalVersion([string]$file) {
    if (-not (Test-Path $file)) { return "" }
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        $i = $line.IndexOf("Internal Version")
        if ($i -ge 0 -and $line.IndexOf(":", $i) -ge 0) { return $line.Substring($line.IndexOf(":", $i) + 1).Trim() }
    }
    return ""
}

Write-Output "Publishing $source"
Write-Output "         to $dest $(if ($DryRun) { '(DRY RUN)' })"
Write-Output ""

# 1. Version must change, and README must agree with it.
$devVersion = Get-InternalVersion "$source\VERSION.txt"
$relVersion = Get-InternalVersion "$dest\VERSION.txt"
if (-not $devVersion) { Refuse "no 'Internal Version' line in VERSION.txt." }
if ($devVersion -eq $relVersion) {
    Refuse "VERSION.txt still says $devVersion, the same as the release. Colleagues only update when the version changes, so nobody would get this. Bump Internal Version (e.g. 10.05 -> 10.06) and add a changelog entry."
}
if (-not (Select-String -Path "$source\README.txt" -SimpleMatch "Version $devVersion" -Quiet)) {
    Refuse "README.txt's header doesn't say 'Version $devVersion'."
}
Write-Output "[ok] version $relVersion -> $devVersion"

# 2. Exes must be built from the current sources.
$built = @{ 'RVTool.cs' = 'RVTool.exe'; 'PMFNotify.cs' = 'PMFNotify.exe'; 'SetupFirewall.cs' = 'SetupFirewall.exe'; 'PMFRoomServer.cs' = 'Server\PMFRoomServer.exe' }
foreach ($cs in $built.Keys) {
    $exe = Join-Path $source $built[$cs]
    if (-not (Test-Path $exe) -or (Get-Item "$source\Source\$cs").LastWriteTime -gt (Get-Item $exe).LastWriteTime) {
        Refuse "Source\$cs is newer than $($built[$cs]) -- run Source\build.ps1 first."
    }
}
$misplaced = @(Get-ChildItem $source -File | Where-Object { $_.Name -eq 'PMFRoomServer.exe' -or $_.Extension -in '.cs', '.manifest' } | ForEach-Object Name)
if ($misplaced) { Refuse "dev-only files in the root would be sent to everyone: $($misplaced -join ', '). Sources belong in Source\, the room server in Server\." }
Write-Output "[ok] exes are built from current sources"

# 3. PMF plugins must at least parse (RV's own Python 3.11, isolated, no .pyc written).
$pyFiles = @(Get-ChildItem "$source\app\PlugIns\Python\pmf_*.py" | ForEach-Object FullName)
& "$source\app\bin\python.exe" -I -B -c "import ast, sys`nfor f in sys.argv[1:]:`n    ast.parse(open(f, encoding='utf-8').read(), f)" @pyFiles
if ($LASTEXITCODE -ne 0) { Refuse "a PMF plugin has a Python syntax error (see above)." }
Write-Output "[ok] $($pyFiles.Count) PMF plugins parse"

# 4. Refresh each pmf_*.rvpkg from its .py and check RV is told to load it.
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$rvload = [IO.File]::ReadAllText("$source\app\PlugIns\Mu\rvload2")
foreach ($pkg in Get-ChildItem "$source\app\PlugIns\Packages\pmf_*.rvpkg") {
    if ($rvload -notmatch "(?m)^[^,]+,$([regex]::Escape($pkg.Name)),") { Refuse "$($pkg.Name) has no line in app\PlugIns\Mu\rvload2, so RV would never load it." }
    $zip = [IO.Compression.ZipFile]::Open($pkg.FullName, $(if ($DryRun) { 'Read' } else { 'Update' }))
    try {
        foreach ($entry in @($zip.Entries | Where-Object { $_.FullName -like '*.py' })) {
            $py = "$source\app\PlugIns\Python\$($entry.FullName)"
            if (-not (Test-Path $py)) { continue }
            $reader = New-Object IO.BinaryReader($entry.Open())
            $inZip = $reader.ReadBytes([int]$entry.Length); $reader.Close()
            $onDisk = [IO.File]::ReadAllBytes($py)
            if ([Convert]::ToBase64String($inZip) -eq [Convert]::ToBase64String($onDisk)) { continue }
            Write-Output "     refreshing $($pkg.Name) <- $($entry.FullName)$(if ($DryRun) { ' (dry run: not changed)' })"
            if (-not $DryRun) {
                $name = $entry.FullName
                $entry.Delete()
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $py, $name) | Out-Null
            }
        }
    } finally { $zip.Dispose() }
}
Write-Output "[ok] plugin packages match their .py files"

# 5. Nothing may be running from the release folder (locked files fail the copy).
$prefix = (Resolve-Path $dest).Path.TrimEnd('\') + '\'
$running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) })
if ($running) { Refuse "running from the release folder: $(($running | ForEach-Object { $_.Name + ' (' + $_.Id + ')' }) -join ', '). Close it first." }
Write-Output "[ok] nothing running from the release folder"
Write-Output ""

# 6. Mirror everything except VERSION.txt, then VERSION.txt last.
#    Dev-only:  Source\ Server\ .claude\ publish_release.ps1, logs, *.old/*.new
$robo = @($source, $dest, '/MIR', '/R:1', '/W:1', '/NDL', '/NP',
    '/XF', 'VERSION.txt', 'publish_release.ps1', 'firewall_log.txt', 'update_log.txt', 'notify_launch_log.txt', 'room_server_log.txt', 'release_manifest.txt', '*.old', '*.new', '*.pmfpart',
    '/XD', "$source\.claude", "$source\Source", "$source\Server")
if ($DryRun) { $robo += '/L' }
robocopy @robo
$code = $LASTEXITCODE
if ($code -ge 8) { Refuse "robocopy failed (exit code $code) -- see above. VERSION.txt was NOT updated, so no colleague will pick up this partial copy." }

if ($DryRun) {
    Write-Output ""
    Write-Output "DRY RUN finished -- nothing was changed. VERSION.txt would go out last ($devVersion)."
    Finish 0
}

# File list for the fast updater (RVTool.exe 10.06+): written after the mirror,
# before VERSION.txt, so it always describes the files that are there.
& "$source\Source\write_manifest.ps1" -Folder $dest
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { Refuse "could not write release_manifest.txt -- VERSION.txt was NOT updated." }

Copy-Item "$source\VERSION.txt" "$dest\VERSION.txt" -Force
Write-Output ""
Write-Host "PUBLISHED $devVersion. Colleagues get it the next time they open PMF.RV." -ForegroundColor Green
Finish 0
