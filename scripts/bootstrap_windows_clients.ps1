[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$HostsFile,
  [Parameter(Mandatory)] [string]$Installer,
  [switch]$DryRun,
  [string]$RetryResults,
  [ValidateRange(1,16)] [int]$ThrottleLimit = 4,
  [pscredential]$Credential,
  [string]$ExpectedSha256
)

$ErrorActionPreference = 'Stop'

function Get-KsatBootstrapHosts {
  param([string]$Path, [string]$RetryPath)
  $hosts = @(Get-Content -LiteralPath $Path -Encoding utf8 | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
  foreach ($hostName in $hosts) {
    if ($hostName.Length -gt 253 -or $hostName -notmatch '^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$') {
      throw "Invalid hostname in host file: $hostName"
    }
  }
  if (@($hosts | Sort-Object -Unique).Count -ne $hosts.Count) { throw 'The hosts file contains duplicate computer names.' }
  if ($RetryPath) {
    $retry = @(Get-Content -Raw -LiteralPath $RetryPath -Encoding utf8 | ConvertFrom-Json)
    $allowed = @($retry | Where-Object { $_.status -in @('Offline','Failed') } | ForEach-Object { $_.hostname.ToString().ToLowerInvariant() })
    $hosts = @($hosts | Where-Object { $_ -in $allowed })
  }
  return $hosts
}

function Test-KsatBootstrapInstaller {
  param([string]$Path, [string]$Sha256)
  $item = Get-Item -LiteralPath $Path
  if (-not $item.Name.EndsWith('.exe',[StringComparison]::OrdinalIgnoreCase) -or $item.Length -le 0) { throw 'The client installer is invalid.' }
  $actual = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($Sha256 -and $actual -ne $Sha256.ToLowerInvariant()) { throw 'The client installer SHA-256 does not match.' }
  if ($IsWindows) {
    $signature = Get-AuthenticodeSignature -LiteralPath $item.FullName
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) { throw 'The client installer Authenticode signature is invalid.' }
  }
  [ordered]@{ path=$item.FullName; sha256=$actual; size=$item.Length }
}

function Test-KsatRemoteHost {
  param([string]$Hostname, [pscredential]$Credential)
  Resolve-DnsName -Name $Hostname -Type A -ErrorAction Stop | Out-Null
  $session = New-CimSession -ComputerName $Hostname -Credential $Credential
  try {
    $system = Get-CimInstance -CimSession $session -ClassName Win32_ComputerSystem
    $disk = Get-CimInstance -CimSession $session -ClassName Win32_LogicalDisk -Filter "DeviceID='C:'"
    if ($system.SystemType -notmatch 'x64' -or [int64]$disk.FreeSpace -lt 1073741824) { throw 'The client requires x64 Windows and at least 1 GB free on C:.' }
    [ordered]@{ architecture=$system.SystemType; free_bytes=[int64]$disk.FreeSpace }
  } finally { Remove-CimSession -CimSession $session }
}

function Invoke-KsatRemoteInstall {
  param([string]$Hostname, [string]$Installer, [pscredential]$Credential)
  $session = New-PSSession -ComputerName $Hostname -Credential $Credential
  $remoteDir = 'C:\ProgramData\KSAT Client\bootstrap'
  $remoteInstaller = "$remoteDir\KSATClientSetup.exe"
  $taskName = "KSAT Client Bootstrap $([guid]::NewGuid().ToString('N'))"
  try {
    Invoke-Command -Session $session -ScriptBlock { param($dir) New-Item -ItemType Directory -Force -Path $dir | Out-Null; & icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null } -ArgumentList $remoteDir
    Copy-Item -LiteralPath $Installer -Destination $remoteInstaller -ToSession $session -Force
    Invoke-Command -Session $session -ScriptBlock {
      param($name,$exe)
      $action=New-ScheduledTaskAction -Execute $exe -Argument '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
      $trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
      Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
      Start-ScheduledTask -TaskName $name
      $deadline=(Get-Date).AddMinutes(7)
      do { Start-Sleep -Seconds 2; $task=Get-ScheduledTaskInfo -TaskName $name } while ($task.LastTaskResult -eq 267009 -and (Get-Date) -lt $deadline)
      if ($task.LastTaskResult -ne 0) { throw "Installer task failed with code $($task.LastTaskResult)." }
      $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/build' -TimeoutSec 10
      if (-not $health.version) { throw 'Client health response is invalid.' }
      $health.version
    } -ArgumentList $taskName,$remoteInstaller
  } finally {
    try { Invoke-Command -Session $session -ScriptBlock { param($name,$dir) Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue; Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue } -ArgumentList $taskName,$remoteDir } catch {}
    Remove-PSSession -Session $session
  }
}

function Write-KsatBootstrapReports {
  param([object[]]$Results, [string]$Directory)
  $jsonPath=Join-Path $Directory 'bootstrap-results.json'; $csvPath=Join-Path $Directory 'bootstrap-results.csv'
  $jsonTemp="$jsonPath.$([guid]::NewGuid().ToString('N')).tmp"; $csvTemp="$csvPath.$([guid]::NewGuid().ToString('N')).tmp"
  @($Results) | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $jsonTemp -Encoding utf8NoBOM
  @($Results) | Export-Csv -LiteralPath $csvTemp -NoTypeInformation -Encoding utf8NoBOM
  Move-Item -LiteralPath $jsonTemp -Destination $jsonPath -Force; Move-Item -LiteralPath $csvTemp -Destination $csvPath -Force
  [ordered]@{ json=$jsonPath; csv=$csvPath }
}

function Invoke-KsatBootstrapHost {
  param([string]$Hostname,[string]$Installer,[pscredential]$Credential,[switch]$DryRun,[hashtable]$Operations)
  if (-not $Operations) { $Operations=@{ Preflight=${function:Test-KsatRemoteHost}; Install=${function:Invoke-KsatRemoteInstall} } }
  $started=(Get-Date).ToUniversalTime().ToString('o')
  try {
    $null=& $Operations.Preflight $Hostname $Credential
    if ($DryRun) { $status='Ready'; $version=$null } else { $version=& $Operations.Install $Hostname $Installer $Credential; $status='Updated' }
    [pscustomobject][ordered]@{ hostname=$Hostname; status=$status; version=$version; error_code=$null; started_at=$started; finished_at=(Get-Date).ToUniversalTime().ToString('o') }
  } catch {
    $status=if ($_.Exception.Message -match 'DNS|WinRM|RPC|network|unreachable') {'Offline'} else {'Failed'}
    [pscustomobject][ordered]@{ hostname=$Hostname; status=$status; version=$null; error_code='bootstrap_failed'; started_at=$started; finished_at=(Get-Date).ToUniversalTime().ToString('o') }
  }
}

function Invoke-KsatBootstrap {
  param([string[]]$Hosts,[string]$Installer,[pscredential]$Credential,[switch]$DryRun,[int]$ThrottleLimit=4,[hashtable]$Operations)
  if ($Operations) {
    return @($Hosts | ForEach-Object { Invoke-KsatBootstrapHost -Hostname $_ -Installer $Installer -Credential $Credential -DryRun:$DryRun -Operations $Operations })
  }
  $scriptPath=$PSCommandPath
  $jobs=@($Hosts | ForEach-Object {
    $hostName=$_
    Start-ThreadJob -ThrottleLimit $ThrottleLimit -ScriptBlock {
      param($source,$hostName,$installer,$credential,$dryRun)
      . $source -HostsFile $installer -Installer $installer -Credential $credential
      Invoke-KsatBootstrapHost -Hostname $hostName -Installer $installer -Credential $credential -DryRun:$dryRun
    } -ArgumentList $scriptPath,$hostName,$Installer,$Credential,[bool]$DryRun
  })
  try { return @($jobs | Receive-Job -Wait -AutoRemoveJob) }
  finally { $jobs | Where-Object State -ne 'Completed' | Stop-Job -ErrorAction SilentlyContinue; $jobs | Remove-Job -Force -ErrorAction SilentlyContinue }
}

function Main {
  if (-not $ExpectedSha256) {
    $manifest=Join-Path (Split-Path -Parent (Resolve-Path -LiteralPath $Installer)) 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $manifest)) { throw 'SHA256SUMS.txt must be beside the client installer.' }
    $installerName=Split-Path -Leaf $Installer
    $line=@(Get-Content -LiteralPath $manifest -Encoding ascii | Where-Object { $_ -match ('^[0-9a-fA-F]{64}  '+[regex]::Escape($installerName)+'$') })
    if ($line.Count -ne 1) { throw 'The client installer is missing from SHA256SUMS.txt.' }
    $script:ExpectedSha256=$line[0].Substring(0,64)
  }
  $artifact=Test-KsatBootstrapInstaller -Path $Installer -Sha256 $ExpectedSha256
  $hosts=Get-KsatBootstrapHosts -Path $HostsFile -RetryPath $RetryResults
  if ($null -eq $Credential) { $script:Credential=Get-Credential -Message 'Administrator account for KSAT lab clients' }
  if ($null -eq $Credential) { throw 'Administrator credentials are required.' }
  $results=Invoke-KsatBootstrap -Hosts $hosts -Installer $artifact.path -Credential $Credential -DryRun:$DryRun -ThrottleLimit $ThrottleLimit
  $reports=Write-KsatBootstrapReports -Results $results -Directory (Get-Location).Path
  Write-Host "Bootstrap complete. JSON: $($reports.json) CSV: $($reports.csv)"
  if (@($results | Where-Object status -in @('Offline','Failed')).Count) { return 1 }
  return 0
}

if ($MyInvocation.InvocationName -ne '.') { exit (Main) }
