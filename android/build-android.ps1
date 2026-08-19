param(
    [string]$DriveFolderUrl = "",
    [Parameter(Mandatory = $true)][string]$AndroidSdk,
    [Parameter(Mandatory = $true)][string]$JavaHomePath,
    [Parameter(Mandatory = $true)][string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$androidRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $androidRoot
$gradleWrapper = Join-Path $androidRoot "gradlew.bat"

foreach ($requiredPath in @($AndroidSdk, $JavaHomePath, $PythonExecutable, $gradleWrapper)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required build path does not exist: $requiredPath"
    }
}

if ($DriveFolderUrl) {
    Set-Content -LiteralPath (Join-Path $androidRoot "mobile-config.properties") -Encoding UTF8 -Value (
        "drive.folderUrl=" + $DriveFolderUrl.Replace("\\", "\\\\").Replace(":", "\:")
    )
}

$sdkProperty = $AndroidSdk.Replace("\", "/")
$pythonProperty = $PythonExecutable.Replace("\", "/")
Set-Content -LiteralPath (Join-Path $androidRoot "local.properties") -Encoding ASCII -Value "sdk.dir=$sdkProperty"

$env:JAVA_HOME = $JavaHomePath
$env:Path = (Join-Path $JavaHomePath "bin") + [IO.Path]::PathSeparator + $env:Path

Push-Location $androidRoot
try {
    & $gradleWrapper assembleDebug "-Pchaquopy.python=$pythonProperty" --no-daemon
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

$sourceApk = Join-Path $androidRoot "app\build\outputs\apk\debug\app-debug.apk"
$releaseDirectory = Join-Path $repositoryRoot "release"
$releaseApk = Join-Path $releaseDirectory "Aptitude-Lab-Mobile-debug.apk"
New-Item -ItemType Directory -Force -Path $releaseDirectory | Out-Null
Copy-Item -LiteralPath $sourceApk -Destination $releaseApk -Force
Write-Output "APK created: $releaseApk"
