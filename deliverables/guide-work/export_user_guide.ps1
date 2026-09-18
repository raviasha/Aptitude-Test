$ErrorActionPreference = 'Stop'
$guideDirectory = Split-Path -Parent $PSScriptRoot
$guideDocx = Join-Path $guideDirectory 'KSAT_Quick_User_Guide.docx'
$guidePdf = Join-Path $guideDirectory 'KSAT_Quick_User_Guide.pdf'
$guideWord = $null
$guideDocument = $null
try {
    $guideWord = New-Object -ComObject Word.Application
    $guideWord.Visible = $false
    $guideWord.DisplayAlerts = 0
    $guideWord.AutomationSecurity = 3
    $guideDocument = $guideWord.Documents.Open($guideDocx, $false, $true)
    $guideDocument.Repaginate()
    $guideDocument.ExportAsFixedFormat($guidePdf, 17, $false, 0, 0, 1, 1, 0, $true, $true, 1, $true, $true, $false)
    Write-Output "Pages: $($guideDocument.ComputeStatistics(2))"
    Write-Output $guidePdf
} finally {
    if ($null -ne $guideDocument) { $guideDocument.Close(0) }
    if ($null -ne $guideWord) { $guideWord.Quit() }
    if ($null -ne $guideDocument) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($guideDocument) }
    if ($null -ne $guideWord) { [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($guideWord) }
}
