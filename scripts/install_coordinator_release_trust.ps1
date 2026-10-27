param(
    [Parameter(Mandatory=$true)][string]$Certificate,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9A-Fa-f]{40}$')][string]$ExpectedThumbprint,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9A-Fa-f]{64}$')][string]$ExpectedSha256
)
$ErrorActionPreference = 'Stop'
$stream = $null
$cert = $null
$hash = [Security.Cryptography.SHA256]::Create()
try {
    # Hold the public file read-only until Import-Certificate finishes.
    $path = (Resolve-Path -LiteralPath $Certificate -ErrorAction Stop).ProviderPath
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    if ($stream.Length -lt 1 -or $stream.Length -gt 16384) { throw 'Invalid publisher certificate size.' }
    $bytes = New-Object byte[] $stream.Length
    $offset = 0
    while ($offset -lt $bytes.Length) {
        $count = $stream.Read($bytes, $offset, $bytes.Length - $offset)
        if ($count -eq 0) { throw 'Incomplete publisher certificate.' }
        $offset += $count
    }
    $digest = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '')
    if ($digest -ne $ExpectedSha256) { throw 'Publisher certificate SHA-256 mismatch.' }
    $cert = [Security.Cryptography.X509Certificates.X509Certificate2]::new($bytes)
    if ($cert.Thumbprint -ne $ExpectedThumbprint) { throw 'Publisher certificate thumbprint mismatch.' }
    if ($cert.Subject -cne 'CN=KSAT LAB RELEASE SIGNING' -or $cert.HasPrivateKey) { throw 'Unapproved publisher certificate.' }
    $now = [DateTime]::UtcNow
    if ($now -lt $cert.NotBefore.ToUniversalTime() -or $now -gt $cert.NotAfter.ToUniversalTime()) {
        throw 'Publisher certificate is not valid at the current time. Check the PC clock.'
    }
    $purposes = @($cert.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' })
    if ($purposes.Count -ne 1 -or '1.3.6.1.5.5.7.3.3' -notin @($purposes[0].EnhancedKeyUsages | ForEach-Object { $_.Value })) {
        throw 'Publisher certificate must have the code-signing purpose.'
    }
    foreach ($name in @('Root', 'TrustedPublisher')) {
        $store = 'Cert:\LocalMachine\' + $name
        $found = @(Get-ChildItem -LiteralPath $store -ErrorAction Stop | Where-Object { $_.Thumbprint -eq $ExpectedThumbprint })
        if ($found.Count -eq 0) {
            Import-Certificate -FilePath $path -CertStoreLocation $store -ErrorAction Stop | Out-Null
            $found = @(Get-ChildItem -LiteralPath $store -ErrorAction Stop | Where-Object { $_.Thumbprint -eq $ExpectedThumbprint })
            $action = 'added'
        } else { $action = 'existing' }
        if ($found.Count -ne 1 -or [BitConverter]::ToString($hash.ComputeHash($found[0].RawData)).Replace('-', '') -ne $ExpectedSha256) {
            throw "Unable to verify publisher trust in $store. Ask the administrator to check certificate-store permissions and retry setup."
        }
        Write-Output "Publisher trust ${name}: $action"
    }
} finally {
    if ($cert) { $cert.Dispose() }
    if ($stream) { $stream.Dispose() }
    $hash.Dispose()
}
