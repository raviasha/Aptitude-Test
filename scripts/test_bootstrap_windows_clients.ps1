$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$temp=Join-Path ([IO.Path]::GetTempPath()) ("ksat-bootstrap-test-"+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  $hostsFile=Join-Path $temp 'hosts.txt'; Set-Content -LiteralPath $hostsFile -Value @(' LAB-A ','lab-b') -Encoding utf8NoBOM
  $installer=Join-Path $temp 'KSATClientSetup-2.1.0.exe'; Set-Content -LiteralPath $installer -Value 'signed-test-installer' -Encoding ascii -NoNewline
  . (Join-Path $PSScriptRoot 'bootstrap_windows_clients.ps1') -HostsFile $hostsFile -Installer $installer -DryRun
  $hosts=@(Get-KsatBootstrapHosts -Path $hostsFile)
  if (($hosts -join ',') -ne 'lab-a,lab-b') { throw 'hostname normalization failed' }
  Set-Content -LiteralPath $hostsFile -Value @('lab-a','LAB-A') -Encoding utf8NoBOM
  $duplicateRejected=$false; try { Get-KsatBootstrapHosts -Path $hostsFile | Out-Null } catch { $duplicateRejected=$true }
  if (-not $duplicateRejected) { throw 'duplicate hostname was accepted' }
  $credential=[pscredential]::new('admin',(ConvertTo-SecureString 'secret-value' -AsPlainText -Force))
  $script:mutations=0; $ops=@{
    Preflight={ param($hostName,$cred) [ordered]@{architecture='x64';free_bytes=2GB} }
    Install={ param($hostName,$path,$cred) $script:mutations++; '2.1.0' }
  }
  $results=@(Invoke-KsatBootstrap -Hosts @('lab-a','lab-b') -Installer $installer -Credential $credential -DryRun -ThrottleLimit 2 -Operations $ops)
  if ($script:mutations -ne 0 -or @($results|Where-Object status -ne 'Ready').Count) { throw 'dry run performed a mutation' }
  $rendered=$results|ConvertTo-Json
  if ($rendered -match 'secret-value') { throw 'credential leaked into output' }
  $retry=Join-Path $temp 'prior.json'; @([pscustomobject]@{hostname='lab-a';status='Updated'},[pscustomobject]@{hostname='lab-b';status='Failed'})|ConvertTo-Json|Set-Content -LiteralPath $retry -Encoding utf8NoBOM
  Set-Content -LiteralPath $hostsFile -Value @('lab-a','lab-b') -Encoding utf8NoBOM
  if ((@(Get-KsatBootstrapHosts -Path $hostsFile -RetryPath $retry) -join ',') -ne 'lab-b') { throw 'retry selection failed' }
  Write-Output 'bootstrap_windows_clients: PASS'
} finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
