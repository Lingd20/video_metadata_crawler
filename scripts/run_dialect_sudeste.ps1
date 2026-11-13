# 发现「东南 Sudeste」(SP+RJ+MG+ES) 相关 YouTube 频道，并导出视频 URL / crawl TSV。
# 用法:
#   powershell -File scripts\run_dialect_sudeste.ps1
#   powershell -File scripts\run_dialect_sudeste.ps1 -MaxPerQuery 20
#   powershell -File scripts\run_dialect_sudeste.ps1 -Region Nordeste

param(
    [string]$Region = "Sudeste",
    [ValidateSet("geo5", "accent7")]
    [string]$Scheme = "geo5",
    [int]$MaxPerQuery = 40,
    [int]$DaysBack = 90,
    [string]$Cookies = "config\youtube_cookies.txt"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = Get-Location }
Set-Location $Root

$safe = ($Region -replace '[^\w\-]+', '_')
$outCsv = "results\dialect_${safe}_channels.csv"
$outUrls = "results\dialect_${safe}_video_urls.txt"
$outTsv = "results\dialect_${safe}_channels.tsv"

New-Item -ItemType Directory -Force -Path results | Out-Null

$argsList = @(
    "scripts\youtube_dialect.py",
    "--region", $Region,
    "--scheme", $Scheme,
    "--max-per-query", "$MaxPerQuery",
    "--days-back", "$DaysBack",
    "--output", $outCsv,
    "--video-urls-out", $outUrls,
    "--channels-tsv-out", $outTsv
)

if (Test-Path $Cookies) {
    $argsList += @("--cookies", $Cookies)
}

Write-Host "Running: python $($argsList -join ' ')" -ForegroundColor Green
python @argsList
