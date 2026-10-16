# Sandbox tests for the 10.06 updater: file-list (manifest) updates, one update at a time,
# interrupted updates, login message, progress window.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$build = Join-Path $repo "build"
$box = Join-Path $PSScriptRoot "upd"
$pass = 0; $fail = 0
function Check($ok, $what) { if ($ok) { $script:pass++; Write-Output "PASS $what" } else { $script:fail++; Write-Output "FAIL $what" } }
function Log { if (Test-Path "$cli\update_log.txt") { Get-Content "$cli\update_log.txt" -Raw } else { "" } }
function Ver($f) { (Select-String -Path $f -Pattern 'Internal Version').Line.Split(':')[1].Trim() }
function RunTool { $p = Start-Process -FilePath "$cli\RVTool.exe" -PassThru -Wait -WindowStyle Hidden; return $p.ExitCode }
function Manifest { & "$repo\scripts\write_manifest.ps1" -Folder $rel | Out-Null }
function Put($path, $text) { New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null; [IO.File]::WriteAllText($path, $text) }

Get-Process timeout, PMFNotify -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$box*" } | Stop-Process -Force
Start-Sleep -Milliseconds 300
if (Test-Path $box) { Remove-Item $box -Recurse -Force }
$rel = "$box\rel"; $cli = "$box\inst\PMF.RVTOOL"
$env:PMF_RVTOOL_TEST = "1"; $env:PMF_RVTOOL_TEST_SETTINGS_INI = "$box\OpenRV.ini"
Remove-Item Env:PMF_RVTOOL_TEST_SELF, Env:PMF_RVTOOL_TEST_UI -ErrorAction SilentlyContinue

# ---- a release with ~3000 files, and a client that matches it except for a few ----
foreach ($i in 1..3000) { Put "$rel\app\lib\pkg$([math]::Floor($i / 100))\file$i.py" "content $i" }
Put "$rel\app\bin\rv.exe" "dummy"
Put "$rel\app\etc\pmf_studio.ini" "[update]`r`nsources = $rel`r`n"
Put "$rel\app\etc\OpenRV_migrate.ini" "[Sync]`r`nsyncWithContactsContactsV2=false`r`n"
Copy-Item "$build\RVTool.exe", "$build\SetupFirewall.exe" $rel
Copy-Item "C:\Windows\System32\timeout.exe" "$rel\PMFNotify.exe"
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.05"
robocopy $rel $cli /MIR /NFL /NDL /NJH /NJS /NP | Out-Null             # client == release at 10.05
Set-Content "$box\inst\PMF.RVTOOL_update_source.txt" $rel               # deliberate override -> our sandbox release

# ---- 10.06 release: 3 files changed, 1 new file in a new folder, 1 file + 1 folder removed ----
Start-Sleep -Milliseconds 1100
Put "$rel\app\lib\pkg5\file500.py" "CHANGED 500"
Put "$rel\app\lib\pkg9\file900.py" "CHANGED 900 longer"
Put "$rel\app\PlugIns\Python\new_plugin.py" "brand new"
Remove-Item "$rel\app\lib\pkg29\file2950.py"
Remove-Item "$rel\app\lib\pkg30" -Recurse
Copy-Item "$build\RVTool.exe" "$rel\RVTool.exe" -Force; (Get-Item "$rel\RVTool.exe").LastWriteTime = (Get-Date).AddMinutes(3)
(Get-Item "$rel\PMFNotify.exe").LastWriteTime = (Get-Date).AddMinutes(3)
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.06"
Manifest
Set-Content "$cli\stray_user_file.txt" "not in the release"

Write-Output "== 1. fast update from the file list (launcher + Notify running) =="
$notify = Start-Process "$cli\PMFNotify.exe" -ArgumentList "/t 300 /nobreak" -PassThru -WindowStyle Hidden
Start-Sleep -Milliseconds 400
$rc = RunTool
Check ($rc -eq 0) "exit code 0 (got $rc)"
Check ((Ver "$cli\VERSION.txt") -eq "10.06") "version stamped 10.06"
Check ((Log) -match "5 of \d+ files changed") "only the 5 changed files copied (log: $(([regex]::Match((Log), '\d+ of \d+ files changed')).Value))"
Check ((Get-Content "$cli\app\lib\pkg5\file500.py") -eq "CHANGED 500") "changed file updated"
Check (Test-Path "$cli\app\PlugIns\Python\new_plugin.py") "new file in a new folder created"
Check (-not (Test-Path "$cli\app\lib\pkg29\file2950.py")) "file removed upstream is removed"
Check (-not (Test-Path "$cli\app\lib\pkg30")) "folder removed upstream is removed"
Check (-not (Test-Path "$cli\stray_user_file.txt")) "stray file removed (same as before)"
Check ((Get-Item "$cli\RVTool.exe").LastWriteTime -eq (Get-Item "$rel\RVTool.exe").LastWriteTime) "running launcher replaced"
Check ($notify.HasExited) "Notify running old code was stopped"
Check (@(Get-ChildItem "$cli" -Recurse -Filter *.pmfpart).Count -eq 0) "no half-copied files left"
Check (Test-Path "$cli\update_log.txt") "own log kept"

Write-Output "== 2. update cut short -> NOT marked updated; next launch finishes it =="
Start-Sleep -Milliseconds 1100
Put "$rel\app\lib\pkg1\file150.py" "CHANGED 150"
Put "$rel\app\lib\pkg2\file250.py" "CHANGED 250"
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.07"; Manifest
$lock = [IO.File]::Open("$cli\app\lib\pkg2\file250.py", 'Open', 'ReadWrite', 'None')
$rc = RunTool
Check ((Ver "$cli\VERSION.txt") -eq "10.06") "version NOT advanced while a file couldn't be updated"
Check ((Log) -match "Update incomplete") "logged as incomplete"
$lock.Close()
$rc = RunTool
Check ((Ver "$cli\VERSION.txt") -eq "10.07") "next launch completes the update"
Check ((Get-Content "$cli\app\lib\pkg2\file250.py") -eq "CHANGED 250") "the locked file got updated afterwards"

Write-Output "== 3. second launch while an update runs -> says so, doesn't start another =="
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.08"; Manifest
$sha = [Security.Cryptography.SHA1]::Create(); $h = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($cli.ToLowerInvariant()))
$mname = "PMF_RV_Update_" + ([BitConverter]::ToString($h, 0, 8) -replace '-', '')
$mtx = New-Object Threading.Mutex($false, $mname); [void]$mtx.WaitOne()
$before = (Log).Length
$rc = RunTool
Check ($rc -eq 3) "second launch exits with 'already updating' (got $rc)"
Check ((Log).Substring($before) -match "already updating") "logged"
Check ((Ver "$cli\VERSION.txt") -eq "10.07") "no second update started"
$mtx.ReleaseMutex(); $mtx.Dispose()
$rc = RunTool
Check ((Ver "$cli\VERSION.txt") -eq "10.08") "once the first finishes, launching works normally"

Write-Output "== 4. damaged file list -> safe fallback to the full comparison =="
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.09"
Add-Content "$rel\release_manifest.txt" "F|1|1|..\..\outside.txt"
$before = (Log).Length
$rc = RunTool
Check ((Log).Substring($before) -match "unreadable") "bad entry rejected"
Check ((Ver "$cli\VERSION.txt") -eq "10.09") "update still completes (robocopy fallback)"
Check (-not (Test-Path "$box\inst\outside.txt") -and -not (Test-Path "$box\outside.txt")) "nothing written outside the install folder"

Write-Output "== 5. release without a file list (older release) -> full comparison =="
Remove-Item "$rel\release_manifest.txt"; Put "$rel\app\lib\pkg3\file333.py" "CHANGED 333"
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.10"
$rc = RunTool
Check ((Ver "$cli\VERSION.txt") -eq "10.10" -and (Get-Content "$cli\app\lib\pkg3\file333.py") -eq "CHANGED 333") "robocopy path still works"

Write-Output "== 6. share answers but login isn't saved -> clear message =="
Set-Content "$box\inst\PMF.RVTOOL_update_source.txt" "\\127.0.0.1\NoSuchShare_PMF"
$before = (Log).Length
$rc = RunTool
Check ((Log).Substring($before) -match "can't open the share.*reviewshare") "log tells them to save the share login"

Write-Output "== 7. file list with wrong paths (the sandbox accident) -> install untouched =="
Set-Content "$box\inst\PMF.RVTOOL_update_source.txt" $rel
$countBefore = @(Get-ChildItem $cli -Recurse -File).Count
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.12"
$m = Get-Content "$rel\release_manifest.txt" -ErrorAction SilentlyContinue
if (-not $m) { Manifest; $m = Get-Content "$rel\release_manifest.txt" }
($m | ForEach-Object { $_ -replace '^(F\|\d+\|\d+\||D\|0\|0\|)(..)', '$1' }) | Set-Content "$rel\release_manifest.txt" -Encoding utf8   # every path shifted by 2 characters
$before = (Log).Length
$rc = RunTool
Check ((Log).Substring($before) -match "doesn't match the release folder") "wrong list detected before use"
Check (@(Get-ChildItem $cli -Recurse -File).Count -ge $countBefore) "no files deleted ($countBefore before, $(@(Get-ChildItem $cli -Recurse -File).Count) after)"
Check (Test-Path "$cli\app\bin\rv.exe") "rv.exe still there"

Write-Output "== 8. list that leaves out most files -> refuses to delete =="
Manifest
$m = Get-Content "$rel\release_manifest.txt"
$keep = $m | Where-Object { $_ -notmatch '^F\|\d+\|\d+\|app\\lib\\pkg(1|2)\d\\' }   # drop ~2000 files from the list
$keep | Set-Content "$rel\release_manifest.txt" -Encoding utf8
Set-Content "$rel\VERSION.txt" "Internal Version  : 10.13"
$countBefore = @(Get-ChildItem $cli -Recurse -File).Count
$before = (Log).Length
$rc = RunTool
Check ((Log).Substring($before) -match "Refusing to remove") "refused to delete a large part of the install"
Check (@(Get-ChildItem $cli -Recurse -File).Count -eq $countBefore) "no files deleted"
Check ((Ver "$cli\VERSION.txt") -ne "10.13") "version not advanced"
Get-Process timeout -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$box*" } | Stop-Process -Force
Remove-Item Env:PMF_RVTOOL_TEST*
Write-Output "RESULT: $pass passed, $fail failed"
