<#
.SYNOPSIS
  Idempotent installer for schedule-reminder (T0 base).
  Creates the DB (WAL + schema), registers the PT5M heartbeat scheduled task, junctions the skill
  into ~/.claude/skills, and runs health. Safe to re-run.

.PARAMETER NoTask    Skip the scheduled-task registration (DB + junction only).
.PARAMETER NoJunction Skip the ~/.claude/skills junction.
#>
[CmdletBinding()]
param(
  [switch]$NoTask,
  [switch]$NoJunction
)
$ErrorActionPreference = "Stop"

$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path           # .../skills/schedule-reminder/scripts
$SkillDir   = Split-Path -Parent $ScriptsDir                            # .../skills/schedule-reminder
$Reminder   = Join-Path $ScriptsDir "reminder.py"
$Python     = (Get-Command python).Source
$PythonW    = (Get-Command pythonw -ErrorAction SilentlyContinue)
if ($PythonW) { $PythonW = $PythonW.Source } else { $PythonW = $Python }

Write-Host "schedule-reminder installer" -ForegroundColor Cyan
Write-Host "  skill dir: $SkillDir"

# 1) DB init (build tables + WAL + user_version) -------------------------------------------------
Write-Host "[1/4] init DB ..."
& $Python $Reminder init | Out-Null
if ($LASTEXITCODE -ne 0) { throw "DB init failed (rc=$LASTEXITCODE)" }

# 2) Heartbeat scheduled task (single PT5M tick; OS = heartbeat only) ----------------------------
# Use schtasks + XML (hardened) — most portable across Windows PowerShell 5.1 / pwsh 7.
if (-not $NoTask) {
  Write-Host "[2/4] register scheduled task ScheduleReminderTick (PT5M heartbeat) ..."
  $taskName = "ScheduleReminderTick"
  $start    = (Get-Date).ToString("s")
  $user     = "$env:USERDOMAIN\$env:USERNAME"
  $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>schedule-reminder PT5M heartbeat -> reminder.py tick (reconciles due reminders).</Description>
    <URI>\ScheduleReminderTick</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$PythonW</Command>
      <Arguments>"$Reminder" tick</Arguments>
      <WorkingDirectory>$ScriptsDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
  $xmlPath = Join-Path $env:TEMP "ScheduleReminderTick.xml"
  # Task Scheduler XML must be UTF-16 LE (declared encoding above).
  [System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::Unicode)
  try {
    schtasks /Create /TN $taskName /XML "$xmlPath" /F | Out-Null
    if ($LASTEXITCODE -eq 0) {
      Write-Host "      registered ScheduleReminderTick (PT5M; missed-fire catch-up via reconciliation)."
    } else {
      Write-Warning "      schtasks rc=$LASTEXITCODE; DB + skill still usable, run 'reminder.py tick' manually."
    }
  } catch {
    Write-Warning "      could not register task ($_). DB + skill still usable; run tick manually."
  } finally {
    Remove-Item $xmlPath -ErrorAction SilentlyContinue
  }
} else {
  Write-Host "[2/4] -NoTask: skipping scheduled task."
}

# 3) Junction into ~/.claude/skills -------------------------------------------------------------
if (-not $NoJunction) {
  Write-Host "[3/4] junction ~/.claude/skills/schedule-reminder ..."
  $link = Join-Path $HOME ".claude\skills\schedule-reminder"
  $parent = Split-Path -Parent $link
  if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  if (Test-Path $link) {
    Write-Host "      exists, leaving as-is: $link"
  } else {
    New-Item -ItemType Junction -Path $link -Target $SkillDir | Out-Null
    Write-Host "      linked -> $SkillDir"
  }
} else {
  Write-Host "[3/4] -NoJunction: skipping junction."
}

# 4) Health self-check ---------------------------------------------------------------------------
Write-Host "[4/4] health check ..."
$health = & $Python $Reminder health --check-task
Write-Host $health
Write-Host "done." -ForegroundColor Green
