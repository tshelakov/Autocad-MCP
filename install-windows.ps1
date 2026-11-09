param(
    [string]$PythonExecutable = 'python',
    [string]$PackageSource = ($PSScriptRoot + '[com]'),
    [string[]]$AutoCADDirectory = @(),
    [switch]$SkipInstanceBridge,
    [switch]$NoDependencies,
    [switch]$Preview
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows.' }
$setupArgs = @('--install-instance-bridge')
foreach ($item in $AutoCADDirectory) { $setupArgs += @('--autocad-directory', $item) }
if ($Preview) {
    if ($SkipInstanceBridge) { Write-Output 'Would install the MCP package; instance bridge skipped.'; exit 0 }
    & $PythonExecutable (Join-Path $PSScriptRoot 'server.py') @setupArgs --dry-run
    if ($LASTEXITCODE -ne 0) { throw 'Instance bridge preview failed; no package was installed.' }
    Write-Output "Would install MCP with $PythonExecutable from $PackageSource"
    exit 0
}
# pip never changes client MCP settings. Use the same Python/venv for an update.
# Do not kill AutoCAD or running servers: stop/reconnect MCP clients before updating.
$pipArgs = @('-m', 'pip', 'install', '--upgrade')
if ($NoDependencies) { $pipArgs += '--no-deps' }
$pipArgs += $PackageSource
& $PythonExecutable @pipArgs
if ($LASTEXITCODE -ne 0) { throw 'MCP installation failed; instance bridge not changed.' }
& $PythonExecutable -m pip check
if ($LASTEXITCODE -ne 0) { throw 'MCP dependency check failed; instance bridge not changed.' }
if (-not $SkipInstanceBridge) {
    # Run the installed module, not this checkout: package/wheel must carry setup + C# source.
    & $PythonExecutable -I -c 'from backends.instance_setup import setup_main; import sys; sys.exit(setup_main(sys.argv[1:]))' @setupArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'MCP installed, but bridge setup failed. Previous bridge retained; rerun autocad-mcp --install-instance-bridge.'
    }
}
Write-Output 'MCP installed. Client settings preserved. Restart MCP clients and AutoCAD to load the installed bridge.'
