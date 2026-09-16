param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,

    [Parameter(Mandatory = $true)]
    [string]$EntryPoint,

    [string[]]$ArgumentList = @(),

    [switch]$WaitForExit,

    [ValidateRange(1, 600)]
    [int]$WaitTimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

$project = (Resolve-Path -LiteralPath $ProjectPath).Path
if (-not (Test-Path -LiteralPath $project -PathType Container)) {
    throw "ProjectPath musí být existující adresář."
}
$invocationRoot = (Get-Location).Path
if (-not [string]::Equals($project, $invocationRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "ProjectPath musí být právě projekt, ve kterém auditor běží."
}

if (($ArgumentList -join " ") -match '[&|<>^]') {
    throw "Argumenty nesmí obsahovat shellové řídicí znaky."
}

$entryCandidate = if ([IO.Path]::IsPathRooted($EntryPoint)) {
    $EntryPoint
} else {
    Join-Path $project $EntryPoint
}
$entry = (Resolve-Path -LiteralPath $entryCandidate).Path

$projectRoot = [IO.Path]::GetFullPath($project).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$entryFull = [IO.Path]::GetFullPath($entry)
if (-not $entryFull.StartsWith($projectRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "EntryPoint musí ležet uvnitř auditovaného projektu."
}
if (-not (Test-Path -LiteralPath $entryFull -PathType Leaf)) {
    throw "EntryPoint musí být existující soubor."
}

$extension = [IO.Path]::GetExtension($entryFull).ToLowerInvariant()
$filePath = $entryFull
$arguments = @($ArgumentList)
switch ($extension) {
    ".exe" { }
    ".dll" {
        $filePath = (Get-Command dotnet -ErrorAction Stop).Source
        $arguments = @($entryFull) + $arguments
    }
    ".ps1" {
        $filePath = (Get-Command powershell.exe -ErrorAction Stop).Source
        $arguments = @("-NoProfile", "-File", $entryFull) + $arguments
    }
    ".bat" {
        $filePath = (Get-Command cmd.exe -ErrorAction Stop).Source
        $arguments = @("/d", "/c", $entryFull) + $arguments
    }
    ".cmd" {
        $filePath = (Get-Command cmd.exe -ErrorAction Stop).Source
        $arguments = @("/d", "/c", $entryFull) + $arguments
    }
    default { throw "Nepodporovaný typ entrypointu '$extension'. Použij .exe, .dll, .ps1, .bat nebo .cmd." }
}

$startParameters = @{
    FilePath = $filePath
    ArgumentList = $arguments
    WorkingDirectory = $project
    PassThru = $true
}
$stdoutFile = $null
$stderrFile = $null
if ($WaitForExit) {
    $stdoutFile = [IO.Path]::GetTempFileName()
    $stderrFile = [IO.Path]::GetTempFileName()
    $startParameters.RedirectStandardOutput = $stdoutFile
    $startParameters.RedirectStandardError = $stderrFile
}

$process = Start-Process @startParameters
$result = [ordered]@{
    runtime_probe = "ok"
    project_path = $project
    entrypoint = $entryFull
    launched_file = $filePath
    pid = $process.Id
    waited = [bool]$WaitForExit
}

if ($WaitForExit) {
    $finished = $process.WaitForExit($WaitTimeoutSeconds * 1000)
    $result.finished = [bool]$finished
    if ($finished) { $result.exit_code = $process.ExitCode }
    if ($stdoutFile -and (Test-Path -LiteralPath $stdoutFile)) {
        $result.stdout = Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue
    }
    if ($stderrFile -and (Test-Path -LiteralPath $stderrFile)) {
        $result.stderr = Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue
    }
    foreach ($captureFile in @($stdoutFile, $stderrFile)) {
        if ($captureFile -and (Test-Path -LiteralPath $captureFile)) {
            Remove-Item -LiteralPath $captureFile -Force -ErrorAction SilentlyContinue
        }
    }
}

[PSCustomObject]$result | ConvertTo-Json -Compress
