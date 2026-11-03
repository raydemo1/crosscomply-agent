[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $envPath) {
    throw '.env already exists. Keep the existing file and check its credentials manually.'
}

$template = Get-Content -LiteralPath (Join-Path $projectRoot '.env.example') -Raw -Encoding UTF8
$pgPassword = [Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant()
$minioPassword = [Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant()
$content = $template.Replace('change-this-postgres-password', $pgPassword).Replace('change-this-minio-password', $minioPassword)
$content = $content.Replace('sk-your-deepseek-api-key', '').Replace('sk-your-embedding-api-key', '')
$bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($content)
$file = [System.IO.File]::Open($envPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
try { $file.Write($bytes, 0, $bytes.Length) } finally { $file.Dispose() }
Write-Output 'Created .env with matching local credentials. Add API keys only when needed.'
