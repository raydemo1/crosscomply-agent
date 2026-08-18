$ErrorActionPreference = "Stop"
$caseRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$reportDataPath = Join-Path $caseRoot "report\report_data.json"
$reportData = Get-Content -Raw -Encoding UTF8 $reportDataPath | ConvertFrom-Json

$failed = $false
foreach ($material in $reportData.material_hashes) {
    $path = Join-Path $caseRoot ("materials\" + $material.filename)
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Error "缺少材料：$($material.filename)"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $material.sha256) {
        Write-Warning "材料哈希不一致：$($material.filename) expected=$($material.sha256) actual=$actual"
        $failed = $true
    } else {
        Write-Output "OK material $($material.filename) $actual"
    }
}

$renderedReportPath = Join-Path $caseRoot ("report\" + $reportData.rendered_report.filename)
$actualReportHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $renderedReportPath).Hash.ToLowerInvariant()
if ($actualReportHash -ne $reportData.rendered_report.sha256) {
    Write-Warning "报告哈希不一致：expected=$($reportData.rendered_report.sha256) actual=$actualReportHash"
    $failed = $true
} else {
    Write-Output "OK report $($reportData.rendered_report.filename) $actualReportHash"
}

Get-Content -Raw -Encoding UTF8 (Join-Path $caseRoot "review\model_pre_extraction.json") | ConvertFrom-Json | Out-Null
Get-Content -Raw -Encoding UTF8 (Join-Path $caseRoot "review\human_confirmed_facts.json") | ConvertFrom-Json | Out-Null
Get-Content -Raw -Encoding UTF8 (Join-Path $caseRoot "review\rule_decision.json") | ConvertFrom-Json | Out-Null
Get-Content -Raw -Encoding UTF8 (Join-Path $caseRoot "approval\feishu_event_fixtures.json") | ConvertFrom-Json | Out-Null
Write-Output "OK JSON fixtures parsed"

if ($failed) {
    throw "英雄案例完整性校验失败。材料变化后应生成新版本并更新快照，不能静默覆盖哈希。"
}

Write-Output "PASS hero case integrity"
