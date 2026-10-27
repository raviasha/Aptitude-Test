# Test-only boundary: never reads or changes a real certificate store.
param([string]$Helper, [Alias('Fixture')][string]$FixturePath, [switch]$Propagate)
$ErrorActionPreference = 'Stop'
$global:fixture = Get-Content -Raw -LiteralPath $FixturePath | ConvertFrom-Json
$global:imports = @()
$global:reads = @()
$global:stores = @{}
$global:publicCertificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($global:fixture.certificate)
foreach ($name in @('Root','TrustedPublisher')) {
    $global:stores[$name] = @()
    if ($global:fixture.existing -contains $name) { $global:stores[$name] = @($global:publicCertificate) }
}
function Get-ChildItem {
    [CmdletBinding()] param([string]$LiteralPath)
    if ($LiteralPath -notmatch '^Cert:\\LocalMachine\\(Root|TrustedPublisher)$') { throw 'Unsafe test store read' }
    $name = $Matches[1]
    $global:reads += $name
    if ($global:fixture.fail_read -eq $name) { throw 'fixture read denied' }
    $global:stores[$name]
}
function Import-Certificate {
    [CmdletBinding()] param([string]$FilePath, [string]$CertStoreLocation)
    if ($CertStoreLocation -notmatch '^Cert:\\LocalMachine\\(Root|TrustedPublisher)$') { throw 'Unsafe test store mutation' }
    $name = $Matches[1]
    $global:imports += $name
    if ($global:fixture.fail_import -eq $name) { throw 'fixture import denied' }
    if ($global:fixture.missing_after_import -ne $name) {
        $global:stores[$name] += [Security.Cryptography.X509Certificates.X509Certificate2]::new($FilePath)
    }
}
$outcome = 'ok'; $message = ''
try {
    & $Helper -Certificate $global:fixture.certificate -ExpectedThumbprint $global:fixture.thumbprint -ExpectedSha256 $global:fixture.sha256 | Out-Null
} catch {
    if ($Propagate) { throw }
    $outcome = 'error'; $message = $_.Exception.Message + ' ' + $_.ScriptStackTrace
}
[ordered]@{outcome=$outcome; message=$message; reads=@($global:reads); imports=@($global:imports);
    root_count=@($global:stores['Root']).Count; publisher_count=@($global:stores['TrustedPublisher']).Count} | ConvertTo-Json -Compress
