param(
    [Parameter(Mandatory = $true)][string]$AutoCADDirectory,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\..\out\instance-bridge')
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$references = @('accoremgd.dll', 'acdbmgd.dll', 'acmgd.dll') | ForEach-Object {
    $referencePath = Join-Path $AutoCADDirectory $_
    if (-not (Test-Path -LiteralPath $referencePath)) { throw "Missing AutoCAD reference: $_" }
    '/reference:' + $referencePath
}
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4.x C# compiler not found.' }
[void](New-Item -ItemType Directory -Path $OutputDirectory -Force)
$output = Join-Path $OutputDirectory 'AutoCadMcp.InstancePublisher.dll'
& $compiler /nologo /target:library /platform:x64 "/out:$output" $references (Join-Path $PSScriptRoot '..\..\backends\instance_bridge\InstancePublisher.cs')
if ($LASTEXITCODE -ne 0) { throw "Compiler failed with exit code $LASTEXITCODE" }
Get-FileHash -LiteralPath $output -Algorithm SHA256
