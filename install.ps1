$ErrorActionPreference = "Stop"

$LatestReleaseApiUrl = if ($env:A0_LATEST_RELEASE_API_URL) { $env:A0_LATEST_RELEASE_API_URL } else { "https://api.github.com/repos/agent0ai/a0-connector/releases/latest" }
$PythonSpec = if ($env:A0_PYTHON_SPEC) { $env:A0_PYTHON_SPEC } else { "3.11" }
$UvInstallUrl = if ($env:UV_INSTALL_URL) { $env:UV_INSTALL_URL } else { "https://astral.sh/uv/install.ps1" }
$RuntimeConstraintsPath = "constraints/a0-runtime.txt"
$BuildConstraintsPath = "constraints/a0-build.txt"
$ReleaseRawFileUrlBase = "https://raw.githubusercontent.com/agent0ai/a0-connector/refs/tags"

function Resolve-PackageSpec {
    if ($env:A0_PACKAGE_SPEC) {
        return @{
            PackageSpec = $env:A0_PACKAGE_SPEC
            ReleaseTag = $null
        }
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
    return @{
        PackageSpec = "a0 @ https://github.com/agent0ai/a0-connector/archive/refs/tags/$escapedTag.zip"
        ReleaseTag = $tag.Trim()
    }
}

function Get-ReleaseFileUrl([string]$Tag, [string]$Path) {
    $escapedTag = [uri]::EscapeDataString($Tag.Trim())
    return "$ReleaseRawFileUrlBase/$escapedTag/$Path"
}

function Test-Enabled([string]$Value) {
    return @("1", "true", "yes", "on", "enabled") -contains $Value.Trim().ToLowerInvariant()
}

function Resolve-ConstraintSpecs($Target) {
    if ($env:A0_RUNTIME_CONSTRAINTS -and $env:A0_BUILD_CONSTRAINTS) {
        return @{
            Runtime = $env:A0_RUNTIME_CONSTRAINTS
            Build = $env:A0_BUILD_CONSTRAINTS
        }
    }

    if ($Target.ReleaseTag) {
        return @{
            Runtime = Get-ReleaseFileUrl $Target.ReleaseTag $RuntimeConstraintsPath
            Build = Get-ReleaseFileUrl $Target.ReleaseTag $BuildConstraintsPath
        }
    }

    if (Test-Enabled "$env:A0_ALLOW_UNPINNED_UPDATE") {
        return @{
            Runtime = $null
            Build = $null
        }
    }

    throw "A0_PACKAGE_SPEC requires A0_RUNTIME_CONSTRAINTS and A0_BUILD_CONSTRAINTS. Set A0_ALLOW_UNPINNED_UPDATE=1 only for intentional development installs."
}

function Resolve-ConstraintFile([string]$Spec, [string]$Name, [string]$TempDir) {
    if (-not $Spec) {
        return $null
    }

    if ($Spec.StartsWith("http://") -or $Spec.StartsWith("https://")) {
        $target = Join-Path $TempDir $Name
        Invoke-WebRequest -Uri $Spec -OutFile $target
        return $target
    }

    if ($Spec.StartsWith("file://")) {
        $path = ([uri]$Spec).LocalPath
    } else {
        $path = $Spec
    }

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Dependency lock file does not exist: $path"
    }
    return $path
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
$Target = Resolve-PackageSpec

$toolBin = (& uv tool dir --bin).Trim()
if ($toolBin) {
    $env:PATH = "$toolBin;$env:PATH"
}

try {
    uv tool update-shell | Out-Null
} catch {
}

$lockTempDir = Join-Path ([IO.Path]::GetTempPath()) ("a0-install-locks-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $lockTempDir | Out-Null
try {
    $constraintSpecs = Resolve-ConstraintSpecs $Target
    $runtimeConstraints = Resolve-ConstraintFile $constraintSpecs.Runtime "a0-runtime.txt" $lockTempDir
    $buildConstraints = Resolve-ConstraintFile $constraintSpecs.Build "a0-build.txt" $lockTempDir

    $installArgs = @("tool", "install", "--python", $PythonSpec, "--managed-python", "--upgrade-package", "a0")
    if ($runtimeConstraints) {
        $installArgs += @("--constraints", $runtimeConstraints)
    }
    if ($buildConstraints) {
        $installArgs += @("--build-constraints", $buildConstraints)
    }
    if (-not $runtimeConstraints -or -not $buildConstraints) {
        Write-Warning "Installing a0 without dependency locks."
    }
    $installArgs += $Target.PackageSpec

    & uv @installArgs
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed for package spec: $($Target.PackageSpec)"
    }
} finally {
    Remove-Item -LiteralPath $lockTempDir -Recurse -Force -ErrorAction SilentlyContinue
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
