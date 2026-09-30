# Publish the EdgeMind hub at a public https:// URL with a Cloudflare quick tunnel.
#   powershell -ExecutionPolicy Bypass -File tools\public-url.ps1          (Ctrl+C stops it)
# Uses HUB_PORT from .env (Docker) or 8000 (run.bat). HTTP/2 over TCP 443 works on networks that block QUIC.
param([int]$Port = 0)
$root = Split-Path $PSScriptRoot -Parent
if (-not $Port) {
    $Port = 8000
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^HUB_PORT=(\d+)' | Select-Object -First 1
        if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
    }
}
$exe = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $exe) { $exe = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
if (-not (Test-Path $exe)) { Write-Host "Install cloudflared first:  winget install Cloudflare.cloudflared"; exit 1 }
Write-Host "Publishing http://localhost:$Port ... the public URL appears below (https://....trycloudflare.com)"
Write-Host "Share <URL>/m with phones. Admin actions need the operator PIN. Ctrl+C to stop."
& $exe tunnel --no-autoupdate --protocol http2 --url "http://localhost:$Port"
