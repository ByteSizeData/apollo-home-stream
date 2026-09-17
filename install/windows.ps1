<#
.SYNOPSIS
  Apollo Home Stream - one-command setup for the gaming PC (Windows 10/11).

.DESCRIPTION
  Installs what's missing (Python, Git, Apollo, Tailscale), puts the bridge in
  %LOCALAPPDATA%\ApolloHomeStream, opens the firewall for your home network, and registers a
  logon task that starts the bridge hidden, restarts it if it ever stops, and keeps it updated.
  Safe to run again at any time: it only changes what isn't already right.

  One line, from PowerShell:
      irm https://raw.githubusercontent.com/ByteSizeData/apollo-home-stream/main/install/windows.ps1 | iex
  With options:
      & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ByteSizeData/apollo-home-stream/main/install/windows.ps1))) -SteamKey YOURKEY -Pin 2550

.PARAMETER DryRun       Show every step, change nothing.
.PARAMETER SteamKey     Your Steam Web API key (stored on this PC only). Optional.
.PARAMETER Pin          PIN for the bridge (default 2550).
.PARAMETER NoApollo     Don't install Apollo.
.PARAMETER NoTailscale  Don't install Tailscale.
.PARAMETER Uninstall    Remove the logon task and firewall rules (leaves Apollo, Tailscale and your files).
#>
[CmdletBinding()]
param(
  [switch]$DryRun,
  [string]$SteamKey = "",
  [string]$Pin = "2550",
  [switch]$NoApollo,
  [switch]$NoTailscale,
  [switch]$Uninstall,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "ApolloHomeStream"),
  [string]$ForUser = $env:USERNAME,
  [int]$Port = 8777
)

$ErrorActionPreference = "Stop"
$Repo      = "ByteSizeData/apollo-home-stream"
$TaskName  = "Apollo Home Stream bridge"
$FwBridge  = "Apollo Home Stream bridge (TCP $Port, home network)"
$FwTs      = "Tailscale direct connections (UDP 41641)"
$script:Did = @()

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Step($title) { Say ""; Say "== $title" "Cyan" }
function Do-It([string]$what, [scriptblock]$action) {
  if ($DryRun) { Say "   [dry-run] would: $what" "DarkYellow"; return }
  Say "   $what"
  & $action
  $script:Did += $what
}
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Is-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Winget-Has($id) {
  if (-not (Have winget)) { return $false }
  $out = winget list --id $id --exact --accept-source-agreements 2>$null | Out-String
  return $out -match [regex]::Escape($id)
}
function Winget-Install($id, $name) {
  if (Winget-Has $id) { Say "   $name is already installed" "DarkGreen"; return }
  Do-It "install $name (winget $id)" { winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements | Out-Null }
}
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Find-Python {
  Refresh-Path
  foreach ($c in @("python", "py")) {
    $p = Get-Command $c -ErrorAction SilentlyContinue
    if ($p -and $p.Source -notmatch "WindowsApps") { return $p.Source }   # skip the Microsoft Store stub
  }
  $guess = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter python.exe -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
  if ($guess) { return $guess.FullName }
  return $null
}

# ---------------------------------------------------------------- elevation
# Firewall rules need admin. Re-launch elevated once, remembering whose logon the task is for.
if (-not $DryRun -and -not (Is-Admin)) {
  Say "Asking Windows for administrator rights (needed for the firewall rules)..." "Yellow"
  $self = $MyInvocation.MyCommand.Path
  if (-not $self) {                                   # started via `irm | iex`: save a copy so it can be re-run elevated
    $self = Join-Path $env:TEMP "apollo-home-stream-install.ps1"
    Invoke-RestMethod "https://raw.githubusercontent.com/$Repo/main/install/windows.ps1" -OutFile $self
  }
  $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$self`"", "-ForUser", "`"$ForUser`"", "-Pin", "`"$Pin`"", "-Port", $Port, "-InstallDir", "`"$InstallDir`"")
  if ($SteamKey)    { $argList += @("-SteamKey", "`"$SteamKey`"") }
  if ($NoApollo)    { $argList += "-NoApollo" }
  if ($NoTailscale) { $argList += "-NoTailscale" }
  if ($Uninstall)   { $argList += "-Uninstall" }
  Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
  return
}

Say "Apollo Home Stream - setup for this gaming PC" "White"
if ($DryRun) { Say "(dry run: nothing will be changed)" "DarkYellow" }

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
  Step "Removing the logon task and firewall rules"
  Do-It "stop and remove the scheduled task '$TaskName'" {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  }
  Do-It "remove firewall rules" {
    Remove-NetFirewallRule -DisplayName $FwBridge -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName $FwTs -ErrorAction SilentlyContinue
  }
  Say ""; Say "Done. Apollo, Tailscale and $InstallDir were left alone." "Green"
  return
}

# ---------------------------------------------------------------- 1. prerequisites
Step "1/6  Programs"
if (-not (Have winget)) {
  Say "   winget isn't available. Install 'App Installer' from the Microsoft Store, then run this again." "Red"
  if (-not $DryRun) { throw "winget missing" }
}
Winget-Install "Python.Python.3.12" "Python 3.12"
Winget-Install "Git.Git" "Git"
if (-not $NoApollo)    { Winget-Install "ClassicOldSong.Apollo" "Apollo (the stream host)" }
if (-not $NoTailscale) { Winget-Install "Tailscale.Tailscale" "Tailscale (for playing away from home)" }
$Python = Find-Python
if (-not $Python -and -not $DryRun) { throw "Python was installed but isn't on PATH yet - close this window, open a new PowerShell, and run the installer again." }
if (-not $Python) { $Python = "python.exe" }
$PythonW = Join-Path (Split-Path $Python) "pythonw.exe"
if (-not (Test-Path $PythonW)) { $PythonW = $Python }
Say "   Python: $Python" "DarkGray"

# ---------------------------------------------------------------- 2. the code
Step "2/6  The bridge"
Refresh-Path
if (Test-Path (Join-Path $InstallDir ".git")) {
  Do-It "update $InstallDir (git pull)" { git -C $InstallDir pull --ff-only --quiet }
} elseif (Test-Path (Join-Path $InstallDir "bridge\apollo_bridge.py")) {
  Do-It "update $InstallDir (bridge --update)" { & $Python (Join-Path $InstallDir "bridge\apollo_bridge.py") --update }
} elseif (Have git) {
  Do-It "clone the project into $InstallDir" { git clone --quiet "https://github.com/$Repo.git" $InstallDir }
} else {
  Do-It "download the project into $InstallDir" {
    $zip = Join-Path $env:TEMP "apollo-home-stream.zip"
    Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/main" -OutFile $zip
    Expand-Archive $zip -DestinationPath $env:TEMP -Force
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item (Join-Path $env:TEMP "apollo-home-stream-main\*") $InstallDir -Recurse -Force
  }
}
$Bridge = Join-Path $InstallDir "bridge\apollo_bridge.py"
if ($SteamKey) {
  Do-It "store your Steam Web API key on this PC (never shown again)" { & $Python $Bridge --set-steam-key $SteamKey | Out-Null }
}

# ---------------------------------------------------------------- 3. firewall
Step "3/6  Firewall (home network only)"
if (-not $DryRun -and (Get-NetFirewallRule -DisplayName $FwBridge -ErrorAction SilentlyContinue)) {
  Say "   bridge rule already present" "DarkGreen"
} else {
  Do-It "allow TCP $Port on Private networks (your other screens reach the bridge here)" {
    New-NetFirewallRule -DisplayName $FwBridge -Direction Inbound -Protocol TCP -LocalPort $Port -Profile Private -Action Allow | Out-Null
  }
}
if (-not $NoTailscale) {
  if (-not $DryRun -and (Get-NetFirewallRule -DisplayName $FwTs -ErrorAction SilentlyContinue)) {
    Say "   Tailscale rule already present" "DarkGreen"
  } else {
    Do-It "allow UDP 41641 (lets Tailscale connect directly instead of through a slow relay)" {
      New-NetFirewallRule -DisplayName $FwTs -Direction Inbound -Protocol UDP -LocalPort 41641 -Action Allow | Out-Null
    }
  }
}

# ---------------------------------------------------------------- 4. start with Windows, stay running
Step "4/6  Start automatically and stay running"
$Log = Join-Path $InstallDir "bridge.log"
$argLine = "`"$Bridge`" --auto-update --port $Port --pin `"$Pin`" --name `"$env:COMPUTERNAME`" --log `"$Log`""
Do-It "register the logon task '$TaskName' for $ForUser (hidden, restarts itself, never times out)" {
  $action   = New-ScheduledTaskAction -Execute $PythonW -Argument $argLine -WorkingDirectory $InstallDir
  $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $ForUser
  # Interactive logon: Steam only accepts steam:// launches from the signed-in desktop session, so this is a task, not a service.
  $principal = New-ScheduledTaskPrincipal -UserId $ForUser -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
              -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}
Do-It "start it now" {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName $TaskName
}

# ---------------------------------------------------------------- 5. Steam comes back after a reboot
Step "5/6  Steam starts with Windows"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$steamExe = @("${env:ProgramFiles(x86)}\Steam\steam.exe", "$env:ProgramFiles\Steam\steam.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $steamExe) {
  Say "   Steam isn't installed in the usual place - install it and sign in, then run this again." "Yellow"
} elseif (-not $DryRun -and (Get-ItemProperty -Path $runKey -Name "Steam" -ErrorAction SilentlyContinue)) {
  Say "   Steam already starts with Windows" "DarkGreen"
} else {
  Do-It "make Steam start with Windows (silently, in the tray)" { Set-ItemProperty -Path $runKey -Name "Steam" -Value "`"$steamExe`" -silent" }
}

# ---------------------------------------------------------------- 6. check it answers, show the addresses
Step "6/6  Check"
if ($DryRun) {
  Say "   [dry-run] would: wait for http://localhost:$Port/pin and run the bridge self-test" "DarkYellow"
} else {
  $up = $false
  foreach ($i in 1..20) {
    try { $r = Invoke-WebRequest "http://localhost:$Port/pin" -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { $up = $true; break } } catch { Start-Sleep -Milliseconds 750 }
  }
  if ($up) { Say "   the bridge is answering on port $Port" "Green" } else { Say "   the bridge didn't answer yet - see $Log" "Yellow" }
  & $Python $Bridge --self-test --no-network --port ($Port + 1) 2>$null | ForEach-Object { Say "   $_" "DarkGray" }
}

Say ""
Say "All set." "Green"
Say "  At home:        http://$($env:COMPUTERNAME):$Port      PIN $Pin"
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if ($ts) {
  try {
    $st = (& tailscale status --json 2>$null | ConvertFrom-Json)
    if ($st.BackendState -eq "Running" -and $st.Self.DNSName) {
      $dns = $st.Self.DNSName.TrimEnd(".")
      Say "  Anywhere:       http://${dns}:$Port      <- use THIS one on phones and laptops" "White"
      Say "  Make every screen find it automatically: add a GitHub secret named BRIDGE_URL with that address."
    } else {
      Say "  Tailscale is installed but not signed in yet - open it from the Start menu and sign in, then run this again." "Yellow"
    }
  } catch { }
}
Say "  Apollo's settings: https://localhost:47990  (set a username and password on first run)"
Say "  Log file:          $Log"
if ($DryRun) { Say ""; Say "Dry run complete - nothing was changed." "DarkYellow" }
