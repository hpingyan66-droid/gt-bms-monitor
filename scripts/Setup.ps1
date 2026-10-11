param([string]$PythonExe = '', [switch]$WithTests, [switch]$SkipInstall, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $workspace
$venvPython = Join-Path $workspace '.venv\Scripts\python.exe'
$editionFile = Join-Path $workspace 'src\gtbms\edition.json'
try {
    $edition = Get-Content -LiteralPath $editionFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (($edition.schema_version -isnot [int] -and $edition.schema_version -isnot [long]) -or
        $edition.schema_version -ne 1 -or $edition.id -notin @('full_access', 'corporate', 'individual') -or
        @($edition.PSObject.Properties).Count -ne 2) { throw 'Invalid manifest' }
} catch { throw 'Edition manifest is missing or invalid. Extract a verified package into a new folder.' }

function Get-UsablePython([string]$Executable) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    # Skip Store execution aliases during automatic discovery; do not launch an installer.
    $probe = "import json,sys,sysconfig,platform; assert sys.version_info >= (3,12); print(json.dumps({'executable':sys.executable,'version':platform.python_version(),'os_arch':platform.machine(),'python_platform':sysconfig.get_platform()}))"
    try {
        $output = & $Executable -c $probe 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
        return (($output -join '') | ConvertFrom-Json)
    } catch { return $null }
}

$pythonInfo = $null
if (Test-Path -LiteralPath $venvPython) {
    $pythonInfo = Get-UsablePython $venvPython
    if (-not $pythonInfo) {
        throw '既存.venvのPythonを実行できません。元のPythonの場所とバージョンを確認してください。既存環境は変更していません。'
    }
    if (-not $CheckOnly) { Write-Host '既存の.venvを使用します。-PythonExeは新規環境を作成する場合に使用します。' }
} elseif (Test-Path -LiteralPath (Join-Path $workspace '.venv')) {
    throw '既存.venvにPythonがありません。内容を確認・保全してから再セットアップしてください。既存環境は変更していません。'
} elseif ($PythonExe) {
    $pythonInfo = Get-UsablePython $PythonExe
    if (-not $pythonInfo) { throw '指定のPythonを実行できません。Python 3.12以降のpython.exeを指定してください。' }
} else {
    $candidates = [Collections.Generic.List[string]]::new()
    # List installed launcher interpreters instead of requesting an uninstalled version.
    foreach ($launcher in @(Get-Command py -CommandType Application -ErrorAction SilentlyContinue)) {
        try {
            $installed = & $launcher.Source -0p 2>$null
            if ($LASTEXITCODE -eq 0) {
                foreach ($line in $installed) {
                    if ($line -match '([A-Za-z]:\\.+?\.exe)\s*$') { $candidates.Add($Matches[1]) }
                }
            }
        } catch { }
    }
    foreach ($command in @(Get-Command python,python3 -CommandType Application -All -ErrorAction SilentlyContinue)) {
        if ($command.Source -notmatch '\\Microsoft\\WindowsApps\\') { $candidates.Add($command.Source) }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $pythonInfo = Get-UsablePython $candidate
        if ($pythonInfo) { break }
    }
}
if (-not $pythonInfo) {
    throw 'Python 3.12以降をインストールし、-PythonExe にpython.exeのパスを指定してください。'
}
$PythonExe = $pythonInfo.executable
$hasConfig = Test-Path -LiteralPath (Join-Path $workspace 'config.local.json') -PathType Leaf
if (-not $hasConfig) {
    $example = Get-Content -LiteralPath (Join-Path $workspace 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($example.mode -ne 'mock' -or $example.ble_address -ne '' -or $example.sync_enabled -ne $false -or
        $example.gps_enabled -ne $false -or $example.current_direction -ne 'unconfirmed') {
        throw 'The public example must start in mock mode without external transmission or a Bluetooth address.'
    }
}
if ($CheckOnly) {
    [pscustomobject]@{
        edition = $edition.id; python_version = $pythonInfo.version; python_platform = $pythonInfo.python_platform
        environment = $(if (Test-Path -LiteralPath $venvPython) { 'existing' } else { 'create_local_venv' })
        configuration = $(if ($hasConfig) { 'preserve_existing' } else { 'create_mock_example' })
        install_dependencies = -not [bool]$SkipInstall; with_tests = [bool]$WithTests
        changes_applied = $false
    } | ConvertTo-Json -Compress
    return
}
Write-Host ('Python ' + $pythonInfo.version + '; Python platform: ' + $pythonInfo.python_platform + '; OS architecture: ' + $pythonInfo.os_arch)
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
if (-not $SkipInstall) {
    $requirements = if ($WithTests) { 'requirements-dev.txt' } else { 'requirements.txt' }
    & $venvPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $venvPython -m pip install 'setuptools>=77' wheel
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed' }
    & $venvPython -m pip install --no-deps --no-build-isolation -e .
    if ($LASTEXITCODE -ne 0) { throw 'Application installation failed' }
}
if (-not $hasConfig) {
    $target = Join-Path $workspace 'config.local.json'
    $stream = $null
    try {
        $stream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $bytes = [IO.File]::ReadAllBytes((Join-Path $workspace 'config.example.json'))
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } catch [IO.IOException] {
        if ($stream -or -not (Test-Path -LiteralPath $target -PathType Leaf)) { throw }
        # A concurrent setup created it; preserve that configuration.
    } finally { if ($stream) { $stream.Dispose() } }
}
& $venvPython -m gtbms.cli doctor
if ($LASTEXITCODE -ne 0) { throw 'Application validation failed' }
Write-Host ('準備完了。配布版: ' + $edition.id)
Write-Host 'Start.cmdで監視画面を開けます。新規設定は模擬モードです。既存設定は保持しました。実機設定はdocs\HARDWARE_VALIDATION.mdを参照してください。'
