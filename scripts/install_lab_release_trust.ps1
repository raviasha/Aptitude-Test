[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string]$Certificate,
  [Parameter(Mandatory)] [string]$ExpectedThumbprint
)

$ErrorActionPreference = 'Stop'
$certificatePath = (Resolve-Path -LiteralPath $Certificate).Path
$candidate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certificatePath)
$thumbprint = $candidate.Thumbprint.ToUpperInvariant()
if ($thumbprint -ne $ExpectedThumbprint.ToUpperInvariant()) {
  throw 'The KSAT Lab release certificate thumbprint does not match.'
}
if ($candidate.Subject -ne 'CN=KSAT LAB RELEASE SIGNING') {
  throw 'The KSAT Lab release certificate publisher does not match.'
}
if ((Get-Date) -lt $candidate.NotBefore -or (Get-Date) -gt $candidate.NotAfter) {
  throw 'The KSAT Lab release certificate is not currently valid.'
}
Import-Certificate -FilePath $certificatePath -CertStoreLocation 'Cert:\LocalMachine\Root' | Out-Null
Import-Certificate -FilePath $certificatePath -CertStoreLocation 'Cert:\LocalMachine\TrustedPublisher' | Out-Null
Write-Output "KSAT Lab release trust installed: $thumbprint"
