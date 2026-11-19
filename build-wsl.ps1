#Requires -Version 5.1
<#
.SYNOPSIS
  Use WSL (default Ubuntu-24.04) with docker/docker.exe to build this project image.

.PARAMETER Distro
  WSL distro name.

.PARAMETER Tag
  Primary image tag (also tags bettafish_modify:latest).

.PARAMETER SaveTar
  If set, after build runs docker save to this Windows path.

.PARAMETER BuildArgs
  Extra args for docker build, e.g. '--build-arg','UV_INDEX=https://pypi.org/simple'

.PARAMETER LegacyDockerBuild
  If set, runs with DOCKER_BUILDKIT=0 (classic builder). Try when export fails with: failed to solve: Unavailable: EOF (often disk full or BuildKit/daemon hiccup).

.NOTES
  wsl --cd must use a Windows path so Unicode paths (e.g. 源代码) work; using wslpath output for --cd breaks chdir on some locales.
#>
param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$Tag = 'bettafish_modify:1.0.0',
    [string]$SaveTar = '',
    [string[]]$BuildArgs = @(),
    [switch]$LegacyDockerBuild
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

Write-Host "Build context (Windows): $projectRoot" -ForegroundColor Cyan

$dockerBin = $null
$probeErr = $null
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
Write-Host 'Checking Docker via WSL (docker.exe ps / docker ps)...' -ForegroundColor DarkGray
try {
    # Prefer docker.exe (Docker Desktop) when Linux docker is a broken symlink (Input/output error).
    $null = & wsl -d $Distro --cd $projectRoot -- docker.exe ps 2>&1
    if ($LASTEXITCODE -eq 0) { $dockerBin = 'docker.exe' }
    if (-not $dockerBin) {
        $probeErr = & wsl -d $Distro --cd $projectRoot -- docker ps 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) { $dockerBin = 'docker' }
    }
}
finally {
    $ErrorActionPreference = $prevEap
}
if (-not $dockerBin) {
    throw "Docker not usable from WSL. Start Docker Desktop and enable WSL integration for '$Distro'. Probe: $probeErr"
}
Write-Host "Using: $dockerBin" -ForegroundColor Cyan

Write-Host 'Starting docker build (often 15-60+ min; long gaps with no new lines are normal during large downloads)...' -ForegroundColor Yellow
if ($LegacyDockerBuild) {
    Write-Host 'Using legacy builder (DOCKER_BUILDKIT=0).' -ForegroundColor DarkYellow
}
# Optional env for classic builder: wsl ... -- env VAR=val docker.exe build ...
$wslArgs = @('-d', $Distro, '--cd', $projectRoot, '--')
if ($LegacyDockerBuild) { $wslArgs += @('env', 'DOCKER_BUILDKIT=0') }
$wslArgs += @($dockerBin, 'build')
foreach ($a in $BuildArgs) { $wslArgs += $a }
$wslArgs += @('-t', $Tag, '-t', 'bettafish_modify:latest', '.')
$ErrorActionPreference = 'Continue'
$buildCode = 1
try {
    & wsl @wslArgs
    $buildCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = 'Stop'
}
if ($buildCode -ne 0) { throw "docker build failed: exit $buildCode" }

Write-Host "Done: $Tag and bettafish_modify:latest" -ForegroundColor Green
$ErrorActionPreference = 'Continue'
try {
    & wsl -d $Distro --cd $projectRoot -- $dockerBin images bettafish_modify
}
finally {
    $ErrorActionPreference = 'Stop'
}

if ($SaveTar) {
    $tarWin = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($SaveTar)
    Write-Host "docker save -> $tarWin" -ForegroundColor Yellow
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $ErrorActionPreference = 'Continue'
        try {
            & docker save $Tag -o $tarWin
            $saveCode = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = 'Stop' }
    }
    else {
        $tarUnix = (& wsl -d $Distro -e wslpath -a $tarWin).Trim()
        $ErrorActionPreference = 'Continue'
        try {
            & wsl -d $Distro --cd $projectRoot -- $dockerBin save $Tag -o $tarUnix
            $saveCode = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = 'Stop' }
    }
    if ($saveCode -ne 0) { throw "docker save failed: exit $saveCode" }
    Write-Host "Saved: $tarWin" -ForegroundColor Green
}
