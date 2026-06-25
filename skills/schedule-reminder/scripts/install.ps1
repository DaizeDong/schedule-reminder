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
if (-not $NoTask) {
  Write-Host "[2/4] register scheduled task ScheduleReminderTick (PT5M heartbeat) ..."
  $taskName = "ScheduleReminderTick"
  $action   = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$Reminder`" tick"
  $trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Minutes 5) `
                -RepetitionDuration (New-TimeSpan -Days 1)
  $settings = New-ScheduledTaskSettingsSet `
                -StartWhenAvailable `
                -MultipleInstances IgnoreNew `
                -DisallowStartIfOnBatteries:$false `
                -StopIfGoingOnBatteries:$false `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
  try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
      -Settings $settings -Force | Out-Null
    Write-Host "      registered (re-registers daily-style trigger; reconciliation handles catch-up)."
  } catch {
    Write-Warning "      could not register task ($_). DB + skill still usable; run tick manually."
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
