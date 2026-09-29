# Test-only external boundary. Never calls Windows Firewall or netsh.
param([string]$Root, [string]$Kind)
$ErrorActionPreference = 'Stop'
$statePath = Join-Path $Root 'state.json'
$command = [IO.File]::ReadAllText((Join-Path $Root 'command.txt'))
$script:state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
function Save-State {
    $script:state | ConvertTo-Json -Depth 10 -Compress | Set-Content -LiteralPath $statePath -Encoding UTF8
}
$script:state.commands = @($script:state.commands) + @($command)
Save-State

# Execute the installer's real verification script against controlled objects.
function Get-NetFirewallRule {
    [CmdletBinding()] param([string]$DisplayName)
    $found = @($script:state.rules | Where-Object { $_.DisplayName -eq $DisplayName })
    if (($found.Count -eq 0) -and ($ErrorActionPreference -eq 'Stop')) { throw 'No such rule' }
    $found
}
function Get-NetFirewallApplicationFilter {
    [CmdletBinding()] param([Parameter(ValueFromPipeline)]$Rule)
    process { [pscustomobject]@{Program=$Rule.Program} }
}
function Get-NetFirewallPortFilter {
    [CmdletBinding()] param([Parameter(ValueFromPipeline)]$Rule)
    process { [pscustomobject]@{Protocol=$Rule.Protocol; LocalPort=$Rule.LocalPort} }
}
try {
    if ($Kind -eq 'powershell') {
        $prefix = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "'
        if (-not $command.StartsWith($prefix) -or -not $command.EndsWith('"')) { throw 'Unexpected command envelope' }
        Invoke-Expression $command.Substring($prefix.Length, $command.Length - $prefix.Length - 1)
        exit 0
    }
    if ($Kind -ne 'netsh') { throw 'Forbidden external tool' }
    if ($command -notmatch '^advfirewall firewall (set|add|delete) rule (.*)$') { throw 'Unknown mutation' }
    $operation = $Matches[1]; $tail = $Matches[2]
    $oldArgs = @{}; $newArgs = @{}; $currentArgs = $oldArgs
    foreach ($token in [regex]::Matches($tail, '\w+="[^"]*"|\w+=[^ ]+|\bnew\b')) {
        if ($token.Value -eq 'new') { $currentArgs = $newArgs; continue }
        $pair = $token.Value.Split('=', 2)
        if ($currentArgs.ContainsKey($pair[0])) { throw 'Duplicate argument' }
        $currentArgs[$pair[0]] = $pair[1].Trim('"')
    }
    $script:state.mutations += 1
    Save-State
    if ($script:state.fail_at -contains $script:state.mutations) { exit 99 }
    $found = @($script:state.rules | Where-Object { $_.DisplayName -ceq $oldArgs.name -and $_.Program -eq $oldArgs.program })
    if ($operation -eq 'add') {
        if ($found.Count -ne 0) { throw 'Rule already exists' }
        if ($oldArgs.dir -ne 'in' -or $oldArgs.action -ne 'allow' -or $oldArgs.protocol -ne 'TCP' -or
            $oldArgs.profile -ne 'private' -or $oldArgs.enable -ne 'yes') { throw 'Unsafe add' }
        $script:state.rules = @($script:state.rules) + @([pscustomobject]@{
            DisplayName=$oldArgs.name; Program=$oldArgs.program; LocalPort=$oldArgs.localport
            Direction='Inbound'; Action='Allow'; Protocol='TCP'; Profile='Private'; Enabled='True'
        })
    } else {
        if ($found.Count -ne 1) { throw 'Mutation must target one exact rule' }
        if ($operation -eq 'delete') {
            $script:state.rules = @($script:state.rules | Where-Object { $_ -ne $found[0] })
        } else {
            if ($newArgs.profile -ne 'private' -or $newArgs.enable -ne 'yes' -or -not $newArgs.localport) { throw 'Unsafe set' }
            if ($newArgs.name) { $found[0].DisplayName = $newArgs.name }
            $found[0].LocalPort = $newArgs.localport
        }
    }
    Save-State
    exit 0
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $Root 'external-error.txt')
    exit 98
}
