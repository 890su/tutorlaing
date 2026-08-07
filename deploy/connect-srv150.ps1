[CmdletBinding()]
param(
    [string]$ServerHost = "10.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 22,
    [string]$RemoteCommand = "hostname",
    [string]$KeySource = "D:\aibrain\04_projects\brainless\.ssh\id_ed25519"
)

$ErrorActionPreference = "Stop"
$ExpectedFingerprint = "SHA256:3nTHxkZX4jdsRjYxWekVb01RfDZtQOBQ8yhh9n2nA/Q"
$SshUser = "admin890brain"
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$tempDirectory = Join-Path $tempRoot ("tutorlaing-ssh-" + [guid]::NewGuid().ToString("N"))
$resolvedTemp = [System.IO.Path]::GetFullPath($tempDirectory)

if (-not $resolvedTemp.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe temporary path: $resolvedTemp"
}
if (-not (Test-Path -LiteralPath $KeySource -PathType Leaf)) {
    throw "SSH secret reference is unavailable: $KeySource"
}

$tempKey = Join-Path $resolvedTemp "srv150_ed25519"
$sshExitCode = 1

try {
    New-Item -ItemType Directory -Path $resolvedTemp | Out-Null
    Copy-Item -LiteralPath $KeySource -Destination $tempKey
    & icacls.exe $tempKey /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not disable inherited ACLs on the temporary SSH key"
    }
    & icacls.exe $tempKey /grant:r "${env:USERNAME}:(R)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict the temporary SSH key to the current user"
    }

    $fingerprintLine = (& ssh-keygen.exe -lf $tempKey 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $fingerprintLine -notmatch [regex]::Escape($ExpectedFingerprint)) {
        throw "SSH key fingerprint does not match server/srv-150/ssh-admin890brain"
    }

    & ssh.exe `
        -p $Port `
        -i $tempKey `
        -o IdentitiesOnly=yes `
        -o BatchMode=yes `
        -o ConnectTimeout=10 `
        -o StrictHostKeyChecking=yes `
        "${SshUser}@${ServerHost}" `
        $RemoteCommand
    $sshExitCode = $LASTEXITCODE
}
finally {
    if ([System.IO.File]::Exists($tempKey)) {
        [System.IO.File]::Delete($tempKey)
    }
    if ([System.IO.Directory]::Exists($resolvedTemp)) {
        [System.IO.Directory]::Delete($resolvedTemp, $false)
    }
}

exit $sshExitCode
