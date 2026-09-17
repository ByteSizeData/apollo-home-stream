<#
.SYNOPSIS
  Apollo Home Stream - one-command setup for the gaming PC (Windows 10/11).

.DESCRIPTION
  Installs what's missing (Python, Git, Apollo, Tailscale), puts the bridge in
  %LOCALAPPDATA%\ApolloHomeStream, opens the firewall to your home network and your Tailscale
  devices only, and registers a task that starts the bridge hidden at sign-in, checks every
  minute that it is still running, and keeps it updated. It also sets the PC up to stay
  reachable while you are away: Tailscale keeps running when nobody is signed in and updates
  itself, the PC stops going to sleep, and Apollo restarts itself if it ever stops.
  Safe to run again at any time: it only changes what isn't already right.

  One line, from PowerShell:
      irm https://bytesizedata.github.io/apollo-home-stream/install.ps1 | iex
  With options:
      & ([scriptblock]::Create((irm https://bytesizedata.github.io/apollo-home-stream/install.ps1))) -SteamKey YOURKEY

  Only the firewall, the startup task, Tailscale, power and Apollo-service steps run as
  administrator (one Windows prompt). Your files, your Steam key and Steam's own startup entry
  are handled as you, so nothing in your profile ends up owned by the administrator.

.PARAMETER DryRun       Show every step, change nothing.
.PARAMETER Status       Read-only report: is everything still in place and running?
.PARAMETER SteamKey     Your Steam Web API key (stored on this PC only). Optional.
.PARAMETER Pin          PIN for the bridge (default 2550).
.PARAMETER NoApollo     Don't install Apollo.
.PARAMETER NoTailscale  Don't install or configure Tailscale.
.PARAMETER KeepSleep    Leave the PC's sleep settings alone (default: never sleep while plugged in).
.PARAMETER AutoLogon    Also set up automatic sign-in after a power cut. Opens Microsoft's Autologon tool;
                        you type your own password into it - this script never sees it.
.PARAMETER Uninstall    Remove the startup task and firewall rules (leaves Apollo, Tailscale and your files).
#>
param(
  [switch]$DryRun,
  [switch]$Status,
  [string]$SteamKey = "",
  [string]$Pin = "2550",
  [switch]$NoApollo,
  [switch]$NoTailscale,
  [switch]$KeepSleep,
  [switch]$AutoLogon,
  [switch]$Uninstall,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "ApolloHomeStream"),
  [string]$ForUser = ([Security.Principal.WindowsIdentity]::GetCurrent().Name),
  [int]$Port = 8777,
  [string]$RepoUrl = "",
  [ValidateSet("All", "Admin")][string]$Stage = "All",
  [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"
$Repo      = "ByteSizeData/apollo-home-stream"
$SiteUrl   = "https://bytesizedata.github.io/apollo-home-stream"
$TaskName  = "Apollo Home Stream bridge"
$FwBridge  = "Apollo Home Stream bridge (TCP $Port, home network + Tailscale)"
$FwBridgeOld = "Apollo Home Stream bridge (TCP $Port, home network)"
$FwTs      = "Tailscale direct connections (UDP 41641)"
$FwFrom    = @("LocalSubnet", "100.64.0.0/10", "fd7a:115c:a1e0::/48")     # this network + your Tailscale devices, nobody else
$TsAdmin   = "https://login.tailscale.com/admin/machines"
$BridgeLog = Join-Path $InstallDir "bridge.log"
$BridgePy  = Join-Path $InstallDir "bridge\apollo_bridge.py"
if (-not $RepoUrl) { $RepoUrl = "https://github.com/$Repo.git" }
$script:Problems = @()      # things that did not work - the summary never says "All set" over these
$script:Todo     = @()      # things only the owner can do

if ($Pin -notmatch '^[A-Za-z0-9_\-]{0,64}$') { throw "The PIN can only contain letters, digits, - and _ ." }
if ($InstallDir -match '["`$]') { throw "The install folder can't contain quotes or dollar signs." }

# ---------------------------------------------------------------- helpers
function Say($msg, $color = "Gray") {
  Write-Host $msg -ForegroundColor $color
  if ($LogFile) { try { Add-Content -Path $LogFile -Value $msg -Encoding UTF8 } catch { } }
}
function Step($title) { Say ""; Say "== $title" "Cyan" }
function Problem($msg) { $script:Problems += $msg; Say "   !! $msg" "Red" }
function Todo($msg) { $script:Todo += $msg }
function Do-It([string]$what, [scriptblock]$action) {
  if ($DryRun) { Say "   [dry-run] would: $what" "DarkYellow"; return $true }
  Say "   $what"
  try { & $action | Out-Null; return $true }
  catch { Problem "couldn't $what - $($_.Exception.Message)"; return $false }
}
function Native([scriptblock]$cmd) {
  # Windows PowerShell 5.1 turns anything a program writes to stderr into a script-ending error under 'Stop'.
  # Run programs relaxed and hand back plain text; $LASTEXITCODE still says how it went.
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"; $out = @()
  try { $out = @(& $cmd 2>&1 | ForEach-Object { "$_" }) } catch { $out = @("$_"); $global:LASTEXITCODE = 1 } finally { $ErrorActionPreference = $old }
  return $out
}
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Is-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Winget-Has($id) {
  if (-not (Have winget)) { return $false }
  $out = (Native { winget list --id $id --exact --accept-source-agreements }) -join "`n"
  return ($LASTEXITCODE -eq 0) -and ($out -match [regex]::Escape($id))
}
function Winget-Install($id, $name, [string[]]$Extra = @(), [switch]$Required) {
  if (Winget-Has $id) { Say "   $name is already installed" "DarkGreen"; return $true }
  if ($DryRun) { Say "   [dry-run] would: install $name (winget $id)" "DarkYellow"; return $true }
  Say "   installing $name ..."
  $wa = @("install", "--id", $id, "--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements") + $Extra
  $out = Native { winget @wa }
  $code = $LASTEXITCODE
  # 0 = installed; the other two mean "already here" / "nothing newer", which is fine.
  if (@(0, -1978335189, -1978335135) -contains $code) { return $true }
  if ($Extra.Count) {                                   # e.g. --scope machine isn't offered: try the plain install
    $wa = @("install", "--id", $id, "--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements")
    $out = Native { winget @wa }
    $code = $LASTEXITCODE
    if (@(0, -1978335189, -1978335135) -contains $code) { return $true }
  }
  $last = ($out | Where-Object { $_.Trim() } | Select-Object -Last 1)
  $msg = "$name did not install (winget exit code $code): $last"
  if ($Required) { throw $msg }
  Problem $msg
  return $false
}
function Find-Python {
  # The real interpreter, never the Microsoft Store stub and never the py.exe launcher - pythonw.exe has to sit beside it.
  Refresh-Path
  foreach ($c in @("py", "python", "python3")) {
    $g = Get-Command $c -ErrorAction SilentlyContinue | Where-Object { $_.Source -and $_.Source -notmatch "WindowsApps" } | Select-Object -First 1
    if (-not $g) { continue }
    $src = $g.Source
    $pa = @("-c", "import sys;print(sys.executable)")
    if ($c -eq "py") { $pa = @("-3") + $pa }
    $exe = (Native { & $src @pa } | Select-Object -Last 1)
    if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
  }
  foreach ($glob in @("$env:ProgramFiles\Python3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe")) {
    $hit = Get-Item $glob -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
    if ($hit) { return $hit.FullName }
  }
  return $null
}
function Find-PythonW($python) {
  if (-not $python) { return $null }
  $dir = Split-Path $python
  if (-not $dir) { return "pythonw.exe" }                  # dry run on a PC that has no Python yet
  $w = Join-Path $dir "pythonw.exe"
  if (Test-Path $w) { return $w }
  return $python
}
function Find-Tailscale {
  $g = Get-Command tailscale -ErrorAction SilentlyContinue
  if ($g) { return $g.Source }
  $p = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
  if (Test-Path $p) { return $p }
  return $null
}
function Tailscale-State {
  $ts = Find-Tailscale
  if (-not $ts) { return $null }
  try { return ((Native { & $ts status --json }) -join "`n" | ConvertFrom-Json) } catch { return $null }
}
function Key-ExpiryDays($st) {
  try {
    if ($st -and $st.Self -and $st.Self.KeyExpiry) { return [int](([datetime]$st.Self.KeyExpiry).ToUniversalTime() - [datetime]::UtcNow).TotalDays }
  } catch { }
  return $null
}
function Bridge-Answers([int]$seconds) {
  $until = (Get-Date).AddSeconds($seconds)
  do {
    try { $r = Invoke-WebRequest "http://127.0.0.1:$Port/pin" -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { return $true } } catch { }
    Start-Sleep -Milliseconds 750
  } while ((Get-Date) -lt $until)
  return $false
}
function Task-ArgLine { return "`"$BridgePy`" --auto-update --port $Port --pin `"$Pin`" --name `"$env:COMPUTERNAME`" --log `"$BridgeLog`"" }

# ================================================================ the administrator part
function Invoke-AdminStage {
  if ($Uninstall) {
    Step "Removing the startup task and firewall rules"
    Do-It "stop and remove the scheduled task '$TaskName'" {
      Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    } | Out-Null
    Do-It "remove firewall rules" {
      foreach ($n in @($FwBridge, $FwBridgeOld, $FwTs)) { Remove-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue }
    } | Out-Null
    Say "   (sleep settings, Tailscale, Apollo and $InstallDir were left alone)" "DarkGray"
    return
  }

  Step "1/7  Programs"
  $haveApollo = [bool](Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "apollo" -or $_.DisplayName -match "apollo" })
  $need = @()
  if (-not (Find-Python)) { $need += "Python" }
  if (-not (Have git)) { $need += "Git" }
  if (-not $NoApollo -and -not $haveApollo) { $need += "Apollo" }
  if (-not $NoTailscale -and -not (Find-Tailscale)) { $need += "Tailscale" }
  if ($need.Count -and -not (Have winget)) {
    Say "   winget isn't available (needed for: $($need -join ', ')). Install 'App Installer' from the Microsoft Store, then run this again." "Red"
    if (-not $DryRun) { throw "winget is missing" }
  }
  if ($need -contains "Python") { Winget-Install "Python.Python.3.12" "Python 3.12" -Extra @("--scope", "machine") -Required | Out-Null } else { Say "   Python is already installed" "DarkGreen" }
  Refresh-Path
  if ($need -contains "Git") { Winget-Install "Git.Git" "Git" | Out-Null } else { Say "   Git is already installed" "DarkGreen" }
  if ($need -contains "Apollo") { Winget-Install "ClassicOldSong.Apollo" "Apollo (the stream host)" | Out-Null } elseif (-not $NoApollo) { Say "   Apollo is already installed" "DarkGreen" }
  if ($need -contains "Tailscale") { Winget-Install "Tailscale.Tailscale" "Tailscale (for playing away from home)" | Out-Null } elseif (-not $NoTailscale) { Say "   Tailscale is already installed" "DarkGreen" }
  $python = Find-Python
  if (-not $python -and -not $DryRun) { throw "Python was installed but can't be found yet - close this window, open a new PowerShell, and run the installer again." }
  if (-not $python) { $python = "python.exe" }
  $pythonW = Find-PythonW $python
  Say "   Python: $python" "DarkGray"

  Step "2/7  Firewall (your home network and your Tailscale devices only)"
  Do-It "allow TCP $Port from this network and from Tailscale (works even if Windows calls your Wi-Fi 'Public')" {
    foreach ($n in @($FwBridge, $FwBridgeOld)) { Remove-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue }
    New-NetFirewallRule -DisplayName $FwBridge -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Any -RemoteAddress $FwFrom -Action Allow
  } | Out-Null
  if (-not $DryRun) {
    # A "Cancel" on Windows' own firewall pop-up leaves a Block rule for Python behind, and Block always beats Allow.
    foreach ($exe in @($python, $pythonW) | Select-Object -Unique) {
      try {
        Get-NetFirewallApplicationFilter -Program $exe -ErrorAction SilentlyContinue | Get-NetFirewallRule -ErrorAction SilentlyContinue |
          Where-Object { $_.Action -eq "Block" -and $_.Direction -eq "Inbound" } | ForEach-Object { Say "   removing a leftover block rule for $exe"; $_ | Remove-NetFirewallRule }
      } catch { }
    }
  }
  if (-not $NoTailscale) {
    if (-not $DryRun -and (Get-NetFirewallRule -DisplayName $FwTs -ErrorAction SilentlyContinue)) { Say "   Tailscale rule already present" "DarkGreen" }
    else {
      Do-It "allow UDP 41641 (lets Tailscale connect directly instead of through a slow relay)" {
        New-NetFirewallRule -DisplayName $FwTs -Direction Inbound -Protocol UDP -LocalPort 41641 -Action Allow
      } | Out-Null
    }
  }

  Step "3/7  Start with Windows, and stay running"
  $argLine = Task-ArgLine
  Do-It "register the task '$TaskName' for $ForUser (hidden; starts at sign-in; re-checked every minute)" {
    $action = New-ScheduledTaskAction -Execute $pythonW -Argument $argLine -WorkingDirectory $InstallDir
    $logon  = New-ScheduledTaskTrigger -AtLogOn -User $ForUser
    $logon.Delay = "PT20S"                                   # let the network come up before the update check
    # The watchdog: fires every minute; with IgnoreNew it does nothing while the bridge runs and restarts it when it doesn't.
    try   { $watch = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1) }
    catch { $watch = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650) }
    # Interactive: Steam only accepts steam:// launches from the signed-in desktop session, so this is a task, not a service.
    $principal = New-ScheduledTaskPrincipal -UserId $ForUser -LogonType Interactive -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -StartWhenAvailable `
                 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $watch) -Principal $principal -Settings $settings -Force
  } | Out-Null

  Step "4/7  Reachable while you are away"
  if ($NoTailscale) { Say "   (skipped: -NoTailscale)" "DarkGray" }
  elseif ($DryRun) {
    Say "   [dry-run] would: keep Tailscale connected when nobody is signed in to Windows, and let it update itself" "DarkYellow"
    Say "   [dry-run] would: if Tailscale isn't signed in, open the sign-in page and wait for you (up to 5 minutes)" "DarkYellow"
  } else {
    $ts = Find-Tailscale
    if (-not $ts) { Refresh-Path; $ts = Find-Tailscale }
    if (-not $ts) { Problem "Tailscale isn't installed, so this PC can't be reached away from home yet." }
    else {
      $st = Tailscale-State
      if (-not $st -or $st.BackendState -ne "Running") {
        Say "   Tailscale needs you to sign in - opening the sign-in page (use the SAME account on your phone and laptop)..." "Yellow"
        $upOut = Join-Path $env:TEMP "apollo-tailscale-up.txt"; $upErr = Join-Path $env:TEMP "apollo-tailscale-up.err.txt"
        $opened = $false; $until = (Get-Date).AddMinutes(5)
        foreach ($verb in @("login", "up")) {                 # `login` needs no flags; `up` is the fallback for old versions
          Remove-Item $upOut, $upErr -ErrorAction SilentlyContinue
          $up = Start-Process $ts -ArgumentList @($verb) -PassThru -NoNewWindow -RedirectStandardOutput $upOut -RedirectStandardError $upErr
          while ((Get-Date) -lt $until) {
            Start-Sleep -Seconds 2
            if (-not $opened) {
              $text = ((Get-Content $upOut, $upErr -ErrorAction SilentlyContinue) -join "`n")
              if ($text -match '(https://login\.tailscale\.com/\S+)') { Start-Process $Matches[1]; $opened = $true; Say "   waiting for you to finish signing in (up to 5 minutes)..." }
            }
            $st = Tailscale-State
            if ($st -and $st.BackendState -eq "Running") { break }
            if ($up.HasExited -and -not $opened) { break }    # this verb didn't produce a sign-in link: try the next one
          }
          if ($up -and -not $up.HasExited) { Stop-Process -Id $up.Id -Force -ErrorAction SilentlyContinue }
          if ($opened -or ($st -and $st.BackendState -eq "Running")) { break }
        }
      }
      if ($st -and $st.BackendState -eq "Running") { Say "   Tailscale is connected as $($st.Self.DNSName.TrimEnd('.'))" "DarkGreen" }
      else { Problem "Tailscale isn't signed in yet. Open Tailscale from the Start menu, sign in, then run this installer again." }
      Native { & $ts set --unattended=true } | Out-Null
      if ($LASTEXITCODE -eq 0) { Say "   Tailscale stays connected even when nobody is signed in to Windows" "DarkGreen" }
      else { Problem "couldn't switch Tailscale to 'run unattended' - turn it on from the Tailscale tray icon > Preferences." }
      Native { & $ts set --auto-update=true } | Out-Null
      if ($LASTEXITCODE -eq 0) { Say "   Tailscale updates itself" "DarkGreen" }
    }
  }

  Step "5/7  Never asleep when you reach for it"
  if ($KeepSleep) { Say "   (skipped: -KeepSleep. The PC can't be woken from outside your home - see the Sleep & wake tab.)" "Yellow" }
  else {
    Do-It "never sleep or hibernate while plugged in (the screen may still turn off)" {
      Native { powercfg /change standby-timeout-ac 0 } | Out-Null
      Native { powercfg /change hibernate-timeout-ac 0 } | Out-Null
    } | Out-Null
  }

  Step "6/7  Apollo restarts itself"
  if ($NoApollo) { Say "   (skipped: -NoApollo)" "DarkGray" }
  elseif ($DryRun) { Say "   [dry-run] would: set Apollo's service to start with Windows and restart itself if it stops" "DarkYellow" }
  else {
    $svc = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "apollo" -or $_.DisplayName -match "apollo" } | Select-Object -First 1
    if (-not $svc) { Problem "Apollo's Windows service wasn't found - install Apollo from https://github.com/ClassicOldSong/Apollo/releases and run this again." }
    else {
      $name = $svc.Name
      Do-It "Apollo service '$name': start with Windows, restart itself after a crash" {
        Set-Service -Name $name -StartupType Automatic
        Native { sc.exe failure $name reset= 86400 actions= restart/5000/restart/5000/restart/60000 } | Out-Null
        if ((Get-Service -Name $name).Status -ne "Running") { Start-Service -Name $name }
      } | Out-Null
    }
  }

  Step "7/7  After a power cut"
  if ($AutoLogon) {
    Say "   Automatic sign-in means anyone who can physically reach this PC gets your desktop without a password." "Yellow"
    Say "   In return, the PC comes all the way back by itself after a power cut. Type your password into Microsoft's" "Yellow"
    Say "   own Autologon window when it opens - this installer never sees it." "Yellow"
    if ($DryRun) { Say "   [dry-run] would: install Microsoft Sysinternals Autologon and open it" "DarkYellow" }
    elseif (Winget-Install "Microsoft.Sysinternals.Autologon" "Microsoft Autologon") {
      Refresh-Path
      $al = @("Autologon64", "Autologon") | ForEach-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
      if ($al) { Start-Process $al.Source -Wait }
      $wl = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
      if ($wl -and "$($wl.AutoAdminLogon)" -eq "1") { Say "   automatic sign-in is on for $($wl.DefaultUserName)" "DarkGreen" }
      else { Problem "automatic sign-in is still off - run Autologon from the Start menu and press Enable." }
    }
  } else {
    Say "   Windows Update restarts sign you back in by themselves. A power cut does not: the PC waits at the" "DarkGray"
    Say "   sign-in screen. You can still get in from the road - open Moonlight/Artemis, pick 'Desktop', and type" "DarkGray"
    Say "   your Windows password there. To skip even that, run this installer again with -AutoLogon." "DarkGray"
  }
}

# ================================================================ the part that runs as you
function Invoke-UserStage {
  if ($Uninstall) { return }
  Step "Your files"
  Refresh-Path
  $python = Find-Python
  if (-not $python) {
    if ($DryRun) { $python = "python.exe" } else { throw "Python can't be found - close this window, open a new PowerShell, and run the installer again." }
  }
  if (Test-Path (Join-Path $InstallDir ".git")) {
    Do-It "update $InstallDir" { $o = Native { git -C $InstallDir pull --ff-only --quiet }; if ($LASTEXITCODE -ne 0) { throw ($o -join " ") } } | Out-Null
  } elseif (Test-Path $BridgePy) {
    Do-It "update $InstallDir" { $o = Native { & $python $BridgePy --update }; if ($LASTEXITCODE -ne 0) { throw ($o | Select-Object -Last 1) } } | Out-Null
  } elseif (Have git) {
    Do-It "download the project into $InstallDir" {
      $tmp = "$InstallDir.download"                          # a leftover folder (say, only a log file) must not block the download
      Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
      $o = Native { git clone --quiet $RepoUrl $tmp }; if ($LASTEXITCODE -ne 0) { throw ($o -join " ") }
      New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
      Get-ChildItem $tmp -Force | Move-Item -Destination $InstallDir -Force
      Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    } | Out-Null
  } else {
    Do-It "download the project into $InstallDir" {
      $zip = Join-Path $env:TEMP "apollo-home-stream.zip"
      Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/main" -OutFile $zip -UseBasicParsing
      Expand-Archive $zip -DestinationPath $env:TEMP -Force
      New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
      Copy-Item (Join-Path $env:TEMP "apollo-home-stream-main\*") $InstallDir -Recurse -Force
    } | Out-Null
  }
  if (-not $DryRun -and -not (Test-Path $BridgePy)) { throw "The project didn't download into $InstallDir - check the internet connection and run this again." }

  if ($SteamKey) {
    Do-It "store your Steam Web API key on this PC (never shown again)" {
      $o = Native { & $python $BridgePy --set-steam-key $SteamKey }; if ($LASTEXITCODE -ne 0) { throw ($o | Select-Object -Last 1) }
    } | Out-Null
  }

  if (-not $KeepSleep) {
    Do-It "tell the bridge to keep this PC awake whenever it runs (change it any time on the Sleep & wake tab)" {
      $o = Native { & $python $BridgePy --set-awake on }; if ($LASTEXITCODE -ne 0) { throw ($o | Select-Object -Last 1) }
    } | Out-Null
  }

  $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
  $steamExe = @("${env:ProgramFiles(x86)}\Steam\steam.exe", "$env:ProgramFiles\Steam\steam.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $steamExe) {
    Say "   Steam isn't installed in the usual place - install it, sign in with 'Remember me', then run this again." "Yellow"
    Todo "Install Steam and sign in with 'Remember me' ticked, then run this installer once more."
  } elseif (-not $DryRun -and (Get-ItemProperty -Path $runKey -Name "Steam" -ErrorAction SilentlyContinue)) {
    Say "   Steam already starts with Windows" "DarkGreen"
  } else {
    Do-It "make Steam start with Windows (silently, in the tray)" { Set-ItemProperty -Path $runKey -Name "Steam" -Value "`"$steamExe`" -silent" } | Out-Null
  }

  Step "Start it and check it"
  if ($DryRun) {
    Say "   [dry-run] would: start the bridge, wait for http://localhost:$Port/pin, run the self-test and the travel check" "DarkYellow"
    return
  }
  try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { Say "   (the task will start by itself within a minute)" "DarkGray" }
  $up = Bridge-Answers 45
  if (-not $up) {
    # No desktop session for the task (or Windows hasn't fired it yet): start it directly so it works right now;
    # the task takes over from the next sign-in.
    Say "   starting the bridge directly this once..." "DarkGray"
    try { Start-Process (Find-PythonW $python) -ArgumentList (Task-ArgLine) -WorkingDirectory $InstallDir -WindowStyle Hidden } catch { }
    $up = Bridge-Answers 45
  }
  if ($up) { Say "   the bridge is answering on port $Port" "Green" } else { Problem "the bridge didn't answer on port $Port - see $BridgeLog" }
  $testArgs = @($BridgePy, "--self-test", "--no-network", "--port", ($Port + 1))
  if (-not $steamExe) { $testArgs += "--allow-missing-steam" }               # already reported above; don't fail the whole setup twice over it
  Native { & $python @testArgs } | ForEach-Object { Say "   $_" "DarkGray" }
  if ($LASTEXITCODE -ne 0) { Problem "the bridge's self-test reported a failure (details above)." }
  Say ""
  Say "   Ready to travel?" "White"
  Native { & $python $BridgePy --preflight --port $Port } | ForEach-Object { Say "   $_" "DarkGray" }
}

# ================================================================ -Status (read-only)
function Show-Status {
  Say "Apollo Home Stream - status of this PC" "White"
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t) {
    $i = $t | Get-ScheduledTaskInfo
    Say ("  startup task:   {0}, {1} triggers, last run {2} (result {3})" -f $t.State, @($t.Triggers).Count, $i.LastRunTime, $i.LastTaskResult)
  } else { Say "  startup task:   MISSING - run the installer again" "Red" }
  $r = Get-NetFirewallRule -DisplayName $FwBridge -ErrorAction SilentlyContinue
  if ($r) { Say "  firewall:       port $Port open to your network and Tailscale" } else { Say "  firewall:       rule missing - run the installer again" "Red" }
  Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { Say ("  network:        {0} ({1})" -f $_.Name, $_.NetworkCategory) }
  if (Bridge-Answers 3) { Say "  bridge:         answering on port $Port" "Green" } else { Say "  bridge:         NOT answering on port $Port" "Red" }
  $python = Find-Python
  if ($python -and (Test-Path $BridgePy)) {
    Say ""
    Native { & $python $BridgePy --preflight --port $Port } | ForEach-Object { Say "  $_" }
  }
  if (Test-Path $BridgeLog) { Say ""; Say "  last lines of $BridgeLog" "DarkGray"; Get-Content $BridgeLog -Tail 15 | ForEach-Object { Say "    $_" "DarkGray" } }
}

# ================================================================ run
if ($Status) { Show-Status; return }

if ($Stage -eq "Admin") {                                  # the elevated child: do the admin part, leave a verdict in the log
  try {
    Invoke-AdminStage
    Say ("RESULT " + $(if ($script:Problems.Count) { "problems" } else { "ok" }))
    foreach ($p in $script:Problems) { Say "PROBLEM $p" }
    exit 0
  } catch {
    Say "   !! $($_.Exception.Message)" "Red"
    Say "RESULT failed"
    Say "PROBLEM $($_.Exception.Message)"
    exit 1
  }
}

Say "Apollo Home Stream - setup for this gaming PC" "White"
if ($DryRun) { Say "(dry run: nothing will be changed)" "DarkYellow" }
$fatal = $null
try {
  if ($DryRun -or (Is-Admin)) {
    Invoke-AdminStage
  } else {
    Say "Windows will ask once for permission (firewall, startup task, Tailscale and power settings need it)..." "Yellow"
    $self = $PSCommandPath
    if (-not $self) {                                       # started via `irm | iex`: save a copy so it can run elevated
      $self = Join-Path $env:TEMP "apollo-home-stream-install.ps1"
      try   { Invoke-WebRequest "$SiteUrl/install.ps1" -OutFile $self -UseBasicParsing }
      catch { Invoke-WebRequest "https://raw.githubusercontent.com/$Repo/main/install/windows.ps1" -OutFile $self -UseBasicParsing }
    }
    $childLog = Join-Path $env:TEMP "apollo-home-stream-install.log"
    Remove-Item $childLog -ErrorAction SilentlyContinue
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$self`"", "-Stage", "Admin", "-LogFile", "`"$childLog`"",
                 "-ForUser", "`"$ForUser`"", "-Pin", "`"$Pin`"", "-Port", $Port, "-InstallDir", "`"$InstallDir`"")
    foreach ($sw in @("NoApollo", "NoTailscale", "KeepSleep", "AutoLogon", "Uninstall")) { if ((Get-Variable $sw -ValueOnly)) { $argList += "-$sw" } }
    try { Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait }
    catch { throw "Windows didn't get your OK, so nothing was changed. Run this again and choose Yes when Windows asks." }
    $lines = @(Get-Content $childLog -ErrorAction SilentlyContinue)
    # The administrator window closes when it finishes - repeat everything it said here so nothing is lost.
    $lines | Where-Object { $_ -notmatch '^(RESULT|PROBLEM) ' } | ForEach-Object { Write-Host $_ }
    $lines | Where-Object { $_ -match '^PROBLEM ' } | ForEach-Object { $script:Problems += ($_ -replace '^PROBLEM ', '') }
    $verdict = ($lines | Where-Object { $_ -match '^RESULT ' } | Select-Object -Last 1)
    if (-not $verdict) { throw "The administrator step didn't finish (its window was closed, or Windows blocked it). Run this again." }
    if ($verdict -eq "RESULT failed") { throw "The administrator step stopped early - see the messages above." }
  }
  Invoke-UserStage
} catch {
  $fatal = $_.Exception.Message
}

# ---------------------------------------------------------------- summary
Say ""
if ($Uninstall -and -not $fatal) { Say "Done. Apollo, Tailscale and $InstallDir were left alone." "Green"; return }
$st = $null
if (-not $NoTailscale -and -not $DryRun) { $st = Tailscale-State }
if ($fatal) {
  Say "SETUP STOPPED: $fatal" "Red"
} elseif ($script:Problems.Count) {
  Say "Setup finished, but $($script:Problems.Count) thing(s) need attention:" "Yellow"
  $script:Problems | ForEach-Object { Say "  - $_" "Yellow" }
} else {
  Say "All set." "Green"
}
if (-not $fatal) {
  Say "  At home:        http://$($env:COMPUTERNAME):$Port      PIN $Pin"
  if ($st -and $st.BackendState -eq "Running" -and $st.Self.DNSName) {
    $dns = $st.Self.DNSName.TrimEnd(".")
    Say "  Anywhere:       http://${dns}:$Port      <- use THIS one on phones and laptops; it works at home too" "White"
    Todo "Make every screen find the PC by itself: on GitHub open the repo > Settings > Secrets and variables > Actions, and add a secret named BRIDGE_URL with the value  http://${dns}:$Port"
    $days = Key-ExpiryDays $st
    if ($null -ne $days) {
      Todo "Tailscale will sign this PC out in $days days unless you switch that off: on $TsAdmin click the ... menu next to $($env:COMPUTERNAME) > 'Disable key expiry'. (Opening that page now.)"
      try { Start-Process $TsAdmin } catch { }
    }
  }
  if (-not $NoApollo) { Say "  Apollo's settings: https://localhost:47990  (set a username and password on first run)" }
  Say "  Log file:          $BridgeLog"
  Say "  Check it any time: run this installer with -Status"
  Todo "In the PC's BIOS/UEFI set 'Restore on AC power loss' to 'Power On', so it comes back by itself after a power cut."
  Todo "Open Steam once and sign in with 'Remember me' ticked."
  if ($script:Todo.Count) {
    Say ""
    Say "Only you can do these (once):" "White"
    $script:Todo | Select-Object -Unique | ForEach-Object { Say "  [ ] $_" }
  }
}
if ($DryRun) { Say ""; Say "Dry run complete - nothing was changed." "DarkYellow" }
if (($fatal -or $script:Problems.Count) -and $PSCommandPath) { exit 1 }      # a real exit code for scripts and CI; never closes an `irm | iex` window
