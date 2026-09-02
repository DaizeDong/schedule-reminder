<#
  act.ps1 - perform ONE whitelisted verb on ONE scheduled task, and report what actually happened.

  The task name arrives in $env:TASKCONSOLE_NAME and the verb in $env:TASKCONSOLE_VERB. They are
  NOT interpolated into a command string anywhere, here or in the caller. That is deliberate: a
  console that builds a PowerShell command out of a name it received over HTTP has an injection
  surface, and the fix is to never put untrusted text where a parser can see it.

  Verbs: enable | disable | run | stop. Nothing else is reachable, and there is no passthrough.

  Reports the state BEFORE and AFTER and whether they differ, rather than reporting the absence of
  an exception. Enable-ScheduledTask on an already-enabled task raises nothing and changes nothing,
  and "no error" would render as success. The caller shows the reader the state transition instead.

  Exit 0 = the verb was applied and the readback confirms it (or the state was already the target).
  Exit 1 = refused or failed, with a reason on stdout.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Emit($ok, $msg, $before, $after) {
  ([ordered]@{ ok = [bool]$ok; message = "$msg"; before = "$before"; after = "$after" } |
    ConvertTo-Json -Compress)
}

$name = $env:TASKCONSOLE_NAME
$verb = $env:TASKCONSOLE_VERB

if ([string]::IsNullOrWhiteSpace($name)) { Emit $false 'no task name supplied' '' ''; exit 1 }
if ($verb -notin @('enable', 'disable', 'run', 'stop')) { Emit $false "verb not allowed: $verb" '' ''; exit 1 }

# Root path only. A console that can reach \Microsoft\Windows\** could disable Windows Update or
# Defender maintenance by typing a name, and nothing here needs that reach.
$t = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
if (-not $t) { Emit $false "no such task at the root path: $name" '' ''; exit 1 }

$before = "$($t.State)"

try {
  switch ($verb) {
    'enable'  { Enable-ScheduledTask  -TaskPath '\' -TaskName $name -ErrorAction Stop | Out-Null }
    'disable' { Disable-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction Stop | Out-Null }
    'run'     { Start-ScheduledTask   -TaskPath '\' -TaskName $name -ErrorAction Stop }
    'stop'    { Stop-ScheduledTask    -TaskPath '\' -TaskName $name -ErrorAction Stop }
  }
} catch {
  # The common real failure is a task owned by SYSTEM or registered at RunLevel=Highest, which a
  # non-elevated console cannot touch. Say that, rather than printing a bare Access Denied.
  # Match the HRESULT, not the message. E_ACCESSDENIED is 0x80070005 in every locale, while the
  # message text is translated, so a string match silently stops working on a non-English Windows
  # and the reader gets a bare error instead of the one hint that explains it. Keeping this file
  # pure ASCII also keeps it out of the PowerShell 5.1 encoding trap: a BOM-less UTF-8 script with
  # non-ASCII in it is decoded as the ANSI codepage and can fail to parse outright.
  $hint = ''
  $denied = ($_.Exception.HResult -eq -2147024891) -or
            ($_.Exception.InnerException -and $_.Exception.InnerException.HResult -eq -2147024891) -or
            ($_.Exception.Message -match 'Access is denied')
  if ($denied) {
    $hint = ' (this task likely needs an elevated console: check its principal, SYSTEM and RunLevel=Highest tasks are not reachable from a normal user session)'
  }
  Emit $false ($_.Exception.Message + $hint) $before $before
  exit 1
}

# Readback. 'run' and 'stop' are asynchronous, so give the scheduler a moment before believing it.
if ($verb -in @('run', 'stop')) { Start-Sleep -Milliseconds 1200 }
$t2 = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
$after = if ($t2) { "$($t2.State)" } else { '(gone)' }

switch ($verb) {
  'enable'  { if ($after -eq 'Disabled') { Emit $false 'still Disabled after Enable' $before $after; exit 1 } }
  'disable' { if ($after -ne 'Disabled') { Emit $false 'not Disabled after Disable' $before $after; exit 1 } }
  # For run/stop the state may already have settled back to Ready by the time we look, so a
  # state comparison cannot prove anything. Say what we saw and let the reader judge from the
  # next refresh (LastRunTime is the honest evidence, and the console re-reads it).
}
Emit $true "$verb applied" $before $after
exit 0
