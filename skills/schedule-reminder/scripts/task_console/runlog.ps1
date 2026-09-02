<#
  runlog.ps1 - read the Task Scheduler Operational log and emit one row per interesting event.

  This is the OTHER history source, and it is a different kind of thing from a health monitor's
  poll log. This one records actual runs: when a task started, when it finished, and what return
  code its action gave. A poll log can only tell you what state a task appeared to be in at the
  moment it was looked at.

  It has one hard limitation that must be reported rather than papered over: THIS LOG ONLY GOES
  BACK TO THE DAY IT WAS ENABLED. It is off by default on Windows. Enabling it does not recover
  history, it starts history. A caller that renders an empty result as "no runs" would be stating
  something we do not know; the correct rendering is "not recorded yet".

  Event IDs kept, and what each one actually means:
    100  task started
    102  task completed successfully
    111  task terminated (by the scheduler, e.g. on hitting its execution time limit)
    201  ACTION completed, and this is the one carrying the real return code
    203  action failed to start
    329  task stopping because its time limit was reached
  102 is the task-level verdict and 201 is the action-level one. They can disagree: an action can
  return non-zero while the task still "completes", so both are emitted and the caller decides.

  Emits UTF-8 JSON on stdout. An empty log is emitted as an empty array with enabled=true, which is
  a different payload from a log that could not be read at all.
#>
[CmdletBinding()]
param([int]$MaxEvents = 20000, [int]$Days = 60)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

$logName = 'Microsoft-Windows-TaskScheduler/Operational'
$wanted = @(100, 102, 111, 201, 203, 329)

$cfg = $null
try { $cfg = Get-WinEvent -ListLog $logName -ErrorAction Stop } catch { }
if (-not $cfg) {
  [ordered]@{ enabled = $false; reason = 'log not present on this machine'; events = @() } |
    ConvertTo-Json -Depth 4 -Compress
  exit 0
}
if (-not $cfg.IsEnabled) {
  [ordered]@{ enabled = $false
              reason  = 'the Task Scheduler Operational log is DISABLED, so there is no run history. Enabling it starts history, it does not recover it.'
              events  = @() } | ConvertTo-Json -Depth 4 -Compress
  exit 0
}

$since = (Get-Date).AddDays(-$Days)
$rows = @()
try {
  $evts = Get-WinEvent -FilterHashtable @{ LogName = $logName; Id = $wanted; StartTime = $since } -MaxEvents $MaxEvents -ErrorAction Stop
} catch {
  # "No events were found" is a normal, expected state for a log enabled five minutes ago, and it
  # is NOT an error. Anything else is.
  if ($_.Exception.Message -match 'No events were found') {
    [ordered]@{ enabled = $true; reason = $null; events = @();
                oldest = $null; note = 'enabled but empty so far' } |
      ConvertTo-Json -Depth 4 -Compress
    exit 0
  }
  [ordered]@{ enabled = $true; reason = "read failed: $($_.Exception.Message)"; events = @() } |
    ConvertTo-Json -Depth 4 -Compress
  exit 0
}

foreach ($e in $evts) {
  $name = $null; $rc = $null
  try {
    $x = [xml]$e.ToXml()
    foreach ($d in $x.Event.EventData.Data) {
      switch ($d.Name) {
        'TaskName'   { $name = $d.'#text' }
        'ResultCode' { $rc = $d.'#text' }
        'ReturnCode' { $rc = $d.'#text' }
      }
    }
  } catch { }
  if (-not $name) { continue }
  $rows += [ordered]@{
    task = ($name -replace '^\\', '')   # the log stores the full task path; the console keys on the bare name
    id   = [int]$e.Id
    # RecordId is the ingester's dedup key. Without it every pass re-inserts everything; with it
    # ALONE the ingester would silently DROP new events after a log clear, because the counter
    # restarts at 1. That is why the log identity below travels with the rows.
    rid  = [int64]$e.RecordId
    t    = $e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')
    rc   = $rc
  }
}

[ordered]@{
  enabled = $true
  reason  = $null
  since   = $since.ToString('yyyy-MM-dd')
  oldest  = if ($rows) { ($rows | Select-Object -Last 1).t } else { $null }
  count   = $rows.Count
  # LOG IDENTITY. OldestRecordNumber going BACKWARDS between two passes is the observable
  # signature of the Operational log having been cleared. The ingester keys its dedup epoch on
  # that, so a restarted RecordId counter cannot make new events look like duplicates.
  oldestRecordId = [int64]$cfg.OldestRecordNumber
  recordCount    = [int64]$cfg.RecordCount
  maxRecordId    = $(if ($rows) { (@($rows | ForEach-Object { $_.rid }) | Measure-Object -Maximum).Maximum } else { 0 })
  events  = $rows
} | ConvertTo-Json -Depth 4 -Compress
