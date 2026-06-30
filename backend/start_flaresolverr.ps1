$ErrorActionPreference = 'Stop'

$Version = "v3.3.21" # Latest stable Windows release version
$ZipName = "flaresolverr_windows_x64.zip"
$DownloadUrl = "https://github.com/FlareSolverr/FlareSolverr/releases/download/$Version/$ZipName"
$InstallDir = "$PSScriptRoot\flaresolverr"

if (-Not (Test-Path "$InstallDir\flaresolverr.exe")) {
    Write-Host "Downloading FlareSolverr $Version..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipName
    
    Write-Host "Extracting..." -ForegroundColor Cyan
    Expand-Archive -Path $ZipName -DestinationPath $InstallDir -Force
    
    # The zip usually contains a "flaresolverr" folder inside it, so we move things up
    if (Test-Path "$InstallDir\flaresolverr\flaresolverr.exe") {
        Move-Item "$InstallDir\flaresolverr\*" "$InstallDir\" -Force
        Remove-Item "$InstallDir\flaresolverr" -Recurse -Force
    }
    
    Remove-Item $ZipName -Force
}

Write-Host "Starting FlareSolverr natively..." -ForegroundColor Green
Set-Location $InstallDir
$env:LOG_LEVEL = "info"
# Port override. The default 8191 falls inside a Windows-reserved TCP port range
# on machines with Hyper-V / WSL2 / Docker Desktop installed (WinError 10013).
# 18191 sits well above the typical excluded ranges. If you change this, also
# update FLARESOLVERR_URL in backend/.env to match.
if (-not $env:PORT) { $env:PORT = "18191" }
Write-Host ("Binding on port {0}" -f $env:PORT) -ForegroundColor Cyan
.\flaresolverr.exe
