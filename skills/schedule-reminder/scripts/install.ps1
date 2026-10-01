<#
.SYNOPSIS
  Plan or install selected capabilities and require measured readiness.
.PARAMETER Capabilities
  Comma-separated store,remind,ingest,work. Omitted means store,remind; empty is a no-op.
.PARAMETER Plan
  Emit a JSON plan without creating files, tasks, or a skill junction.
#>
[CmdletBinding()]
param(
  [AllowEmptyString()][string]$Capabilities = "store,remind",
  [switch]$Plan,
  [switch]$NoTask,
  [switch]$NoJunction
)
$ErrorActionPreference = "Stop"
$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = if ($env:SCHEDULE_PYTHON) { $env:SCHEDULE_PYTHON } else { (Get-Command python).Source }
if ($NoTask -and -not $PSBoundParameters.ContainsKey('Capabilities')) { $Capabilities = 'store' }
$Arguments = @('-B', (Join-Path $ScriptsDir 'installer.py'), "--capabilities=$Capabilities")
if ($Plan) { $Arguments += '--plan' }
if ($NoTask) { $Arguments += '--no-task' }
& $Python @Arguments
$InstallExit = $LASTEXITCODE
if ($InstallExit -ne 0 -or $Plan -or [string]::IsNullOrWhiteSpace($Capabilities)) { exit $InstallExit }
if (-not $NoJunction) {
  $SkillDir = Split-Path -Parent $ScriptsDir
  $Link = Join-Path $HOME '.claude\skills\schedule-reminder'
  if (-not (Test-Path -LiteralPath $Link)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Link) -Force | Out-Null
    New-Item -ItemType Junction -Path $Link -Target $SkillDir | Out-Null
  }
}
exit 0
