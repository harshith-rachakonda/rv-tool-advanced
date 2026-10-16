# Compile the C# components into build\ with the C# compiler that ships with
# Windows (.NET Framework 4.x) -- no SDK needed.
#
#   .\scripts\build.ps1
#
param()
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$out  = Join-Path $repo 'build'
$csc  = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
New-Item -ItemType Directory -Force $out | Out-Null

$targets = [ordered]@{
    'RVTool.exe'        = @('/target:winexe', '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', "$repo\src\launcher\RVTool.cs")
    'PMFNotify.exe'     = @('/target:winexe', '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll', "$repo\src\notify\PMFNotify.cs")
    'SetupFirewall.exe' = @('/target:exe', "/win32manifest:$repo\src\firewall\SetupFirewall.exe.manifest", "$repo\src\firewall\SetupFirewall.cs")
    'PMFRoomServer.exe' = @('/target:winexe', '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', "$repo\src\room-server\PMFRoomServer.cs")
}
$failed = 0
foreach ($exe in $targets.Keys) {
    Write-Output "Building $exe ..."
    & $csc /nologo /optimize+ "/out:$out\$exe" @($targets[$exe])
    if ($LASTEXITCODE -ne 0) { $failed++ }
}
if ($failed) { exit 1 }
