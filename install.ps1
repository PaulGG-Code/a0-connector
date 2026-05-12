$ErrorActionPreference = "Stop"

$LatestReleaseApiUrl = if ($env:A0_LATEST_RELEASE_API_URL) { $env:A0_LATEST_RELEASE_API_URL } else { "https://api.github.com/repos/agent0ai/a0-connector/releases/latest" }
$PythonSpec = if ($env:A0_PYTHON_SPEC) { $env:A0_PYTHON_SPEC } else { "3.11" }
$UvInstallUrl = if ($env:UV_INSTALL_URL) { $env:UV_INSTALL_URL } else { "https://astral.sh/uv/install.ps1" }

function Resolve-PackageSpec {
    if ($env:A0_PACKAGE_SPEC) {
        return $env:A0_PACKAGE_SPEC
    }

    try {
        $headers = @{
            Accept = "application/vnd.github+json"
            "User-Agent" = "a0-cli-installer"
        }
        $release = Invoke-RestMethod -Uri $LatestReleaseApiUrl -Headers $headers
    } catch {
        throw "Could not resolve the latest a0 release from GitHub. Set A0_PACKAGE_SPEC to install from a specific package source. $($_.Exception.Message)"
    }

    $tag = [string]$release.tag_name
    if (-not $tag.Trim()) {
        throw "GitHub latest-release response did not include tag_name. Set A0_PACKAGE_SPEC to install from a specific package source."
    }

    $escapedTag = [uri]::EscapeDataString($tag.Trim())
    return "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/$escapedTag.zip"
}

function Ensure-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        return
    }

    irm $UvInstallUrl | iex

    $localBin = Join-Path $HOME ".local\bin"
    if (Test-Path $localBin) {
        $env:PATH = "$localBin;$env:PATH"
    }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv was installed but is not on PATH in this shell yet. Open a new terminal, then rerun this installer."
    }
}

Ensure-Uv
$PackageSpec = Resolve-PackageSpec

$toolBin = (& uv tool dir --bin).Trim()
if ($toolBin) {
    $env:PATH = "$toolBin;$env:PATH"
}

try {
    uv tool update-shell | Out-Null
} catch {
}

$installArgs = @("tool", "install", "--python", $PythonSpec, "--managed-python", "--upgrade", $PackageSpec)
& uv @installArgs
if ($LASTEXITCODE -ne 0) {
    throw "uv tool install failed for package spec: $PackageSpec"
}

Write-Host ""
Write-Host "a0 is installed."
Write-Host ""
Write-Host "Run:"
Write-Host "  a0"
Write-Host ""
Write-Host "Managed Python:"
Write-Host "  $PythonSpec"
Write-Host ""
if ($toolBin) {
    Write-Host "If 'a0' is not available in your current shell yet, open a new terminal."
    Write-Host "uv installs tool executables in:"
    Write-Host "  $toolBin"
}
