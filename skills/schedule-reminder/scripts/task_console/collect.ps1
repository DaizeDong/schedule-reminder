<#
  collect.ps1 - dump the live Windows Task Scheduler state as JSON on stdout.

  This is the ONLY thing in task-console that talks to the scheduler for reading. It emits raw
  facts and makes no judgement: the merging with a health watch-list, a backup allow-list and a
  category map all happens in server.py, so the rules live in one place and this file stays a
  dumb collector.

  I3 (spec invariant): enumerating zero tasks is a FAILURE, not an empty result. A collector that
  returns [] when the scheduler service is down looks exactly like a machine with no tasks, and the
  console would render a confident empty page. It throws instead.

  Emits UTF-8 without a BOM on stdout. Callers must decode as UTF-8; PowerShell 5.1 in a detached
  session otherwise hands back the ANSI codepage and every non-ASCII label becomes mojibake.
#>
[CmdletBinding()]
param(
  # Tasks whose name matches this are vendor-installed and out of scope for the console.
  [string]$VendorPattern = '^(NVIDIA|OneDrive|Adobe|Zoom|XRite|MicrosoftEdge|Nahimic|Optane|Intel|NvProfile|SoftLanding|RunPlatform|Lenovo|Launch Adobe)'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

$all = @(Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue)
if ($all.Count -eq 0) {
  # Do not soften this into an empty payload. See the header.
  throw 'collect: Get-ScheduledTask enumerated 0 tasks. The scheduler service, CIM, or permissions are broken. This is not an empty machine.'
}

$rows = @()
foreach ($t in $all) {
  if ($t.TaskName -match $VendorPattern) { continue }
  $i = $null
  try { $i = $t | Get-ScheduledTaskInfo -ErrorAction Stop } catch { }
  $s = $t.Settings

  # Triggers are emitted BOTH as a human string and as structured fields. The string is for the
  # table; the structure is what lets the caller lay today's occurrences out on a 24 hour timeline.
  # A summarised string cannot be expanded back into times, and guessing from it would be inventing.
  $trg = @()
  $trgRaw = @()
  foreach ($x in $t.Triggers) {
    $kind = $x.CimClass.CimClassName -replace 'MSFT_Task', '' -replace 'Trigger', ''
    $rep = ''
    if ($x.Repetition -and $x.Repetition.Interval) { $rep = '/' + $x.Repetition.Interval }
    $at = ''
    if ($x.StartBoundary) { try { $at = ' @' + ([datetime]$x.StartBoundary).ToString('HH:mm') } catch { } }
    $trg += ("$kind$rep$at").Trim()

    $props = $x.CimInstanceProperties.Name
    $trgRaw += [ordered]@{
      kind     = $kind
      enabled  = [bool]$x.Enabled
      start    = if ($x.StartBoundary) { "$($x.StartBoundary)" } else { $null }
      end      = if ($x.EndBoundary) { "$($x.EndBoundary)" } else { $null }
      interval = if ($x.Repetition) { "$($x.Repetition.Interval)" } else { $null }
      duration = if ($x.Repetition) { "$($x.Repetition.Duration)" } else { $null }
      days     = if ($props -contains 'DaysInterval')  { [int]$x.DaysInterval }  else { $null }
      weeks    = if ($props -contains 'WeeksInterval') { [int]$x.WeeksInterval } else { $null }
      dow      = if ($props -contains 'DaysOfWeek')    { [int]$x.DaysOfWeek }    else { $null }
    }
  }

  $rows += [ordered]@{
    name        = $t.TaskName
    # The task's OWN description, used as the fallback when the category map has no override.
    # Whitespace is collapsed because these are often multi-line and would wreck a table cell.
    description = if ($t.Description) { ($t.Description -replace '\s+', ' ').Trim() } else { $null }
    state       = "$($t.State)"
    rcRaw       = if ($i) { [int64]$i.LastTaskResult } else { $null }
    rcHex       = if ($i) { '0x{0:X}' -f ($i.LastTaskResult -band 0xFFFFFFFF) } else { $null }
    lastRun     = if ($i -and $i.LastRunTime -gt (Get-Date '2000-01-01')) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { $null }
    nextRun     = if ($i -and $i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { $null }
    triggers    = ($trg -join ', ')
    triggersRaw = $trgRaw
    exec        = $t.Actions[0].Execute
    args        = $t.Actions[0].Arguments
    catchup     = [bool]$s.StartWhenAvailable
    retries     = [int]$s.RestartCount
    timeout     = "$($s.ExecutionTimeLimit)"
    multi       = "$($s.MultipleInstances)"
    # Reported as the CIM property means it, not as the cmdlet's inverted switch name.
    # DisallowStartIfOnBatteries=True means "refuses to start on battery", which is the default.
    refuseOnBattery = [bool]$s.DisallowStartIfOnBatteries
    stopOnBattery   = [bool]$s.StopIfGoingOnBatteries
    runLevel    = "$($t.Principal.RunLevel)"
    userId      = "$($t.Principal.UserId)"
  }
}

[ordered]@{
  generated = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  enumerated = $all.Count
  tasks = $rows
} | ConvertTo-Json -Depth 6 -Compress
