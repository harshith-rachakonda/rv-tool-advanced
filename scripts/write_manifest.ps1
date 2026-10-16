# PMF.RVTOOL - write release_manifest.txt for a release folder
#
#   .\write_manifest.ps1 -Folder C:\PMF.RVTOOL_release
#
# Called by publish_release.ps1 after the mirror and BEFORE VERSION.txt is
# copied. RVTool.exe (10.06+) reads this one file and copies only the files
# whose size or timestamp differ, instead of asking the share about all
# 10,000+ files one by one -- the difference between seconds and many
# minutes over Tailscale.
#
# Format (UTF-8): "F|size|lastWriteUtcTicks|relative\path" per file and
# "D|0|0|relative\dir" per folder. Root-level VERSION.txt, logs and the
# manifest itself are left out (RVTool.exe never touches those).

param([Parameter(Mandatory = $true)][string]$Folder)

$ErrorActionPreference = 'Stop'
# Full long-form path, and .NET enumeration that returns children under exactly
# this prefix: a short (8.3) or differently-cased root would otherwise shift
# every relative path, and RVTool.exe would see nothing it recognises.
$root = [IO.Path]::GetFullPath((Get-Item -LiteralPath $Folder).FullName).TrimEnd('\')
$skipAtRoot = @('VERSION.txt', 'release_manifest.txt', 'update_log.txt', 'firewall_log.txt', 'notify_launch_log.txt')
$skipExt = @('.old', '.new', '.pmfpart')

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add('# PMF.RV release manifest v1 -- F|size|lastWriteUtcTicks|path  /  D|0|0|path')
$files = 0
$dirInfo = New-Object IO.DirectoryInfo($root)
foreach ($item in $dirInfo.EnumerateFileSystemInfos('*', [IO.SearchOption]::AllDirectories)) {
    if (-not $item.FullName.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "unexpected path: $($item.FullName)" }
    $rel = $item.FullName.Substring($root.Length + 1)
    if ($item -is [IO.DirectoryInfo]) {
        $lines.Add("D|0|0|$rel")
    } elseif (($skipAtRoot -notcontains $rel) -and ($skipExt -notcontains $item.Extension.ToLowerInvariant())) {
        $lines.Add("F|$($item.Length)|$($item.LastWriteTimeUtc.Ticks)|$rel")
        $files++
    }
}
foreach ($core in 'RVTool.exe', 'app\bin\rv.exe') {
    $pattern = "^F\|\d+\|\d+\|" + [regex]::Escape($core) + "$"
    if (-not ($lines | Where-Object { $_ -match $pattern })) { throw "release_manifest.txt would not list $core -- not writing it." }
}

$tmp = Join-Path $root 'release_manifest.txt.tmp'
[IO.File]::WriteAllLines($tmp, $lines, (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $tmp -Destination (Join-Path $root 'release_manifest.txt') -Force
Write-Output "release_manifest.txt: $files files"
