# One-time setup for Windows (PowerShell). Needs internet ONCE; offline after.
# Run:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Host ">> Installing Ollama..."
  winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
  Write-Host "   Close and reopen PowerShell, then run this script again."; exit
}

$ramGB = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if ($ramGB -ge 16) { $model = "gemma4:e4b" }
elseif ($ramGB -ge 8) { $model = "qwen3-vl:2b-instruct" }
else { $model = "gemma3:1b"; Write-Host "!! Under 8 GB RAM: tiny text-only model (no images)." }
Write-Host ">> $ramGB GB RAM detected -> pulling $model"
ollama pull $model

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -q --upgrade pip
pip install -q -e ".[dev,media]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

Write-Host ""
Write-Host "Done. Set 'local.model: $model' in config.yaml if it differs, then:"
Write-Host "  .\.venv\Scripts\Activate.ps1; nestcrew doctor; nestcrew serve"
