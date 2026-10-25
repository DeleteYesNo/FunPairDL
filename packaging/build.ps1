# Build the Windows release: dist\FunPairDL-<version>-windows-x64.zip
#
#   python -m pip install -r packaging\requirements-lock.txt
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Python <python.exe>]
#
# The zip holds one FunPairDL\ folder: FunPairDL.exe, its _internal\ code and
# the README / LICENSE / config template. No config.json: the app writes its
# own on first start, next to the exe, so extracting a newer zip over an old
# folder keeps the settings and the queue.
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Select-String -Path funpairdl\__init__.py -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
& $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\FunPairDL.spec
if ($LASTEXITCODE) { throw "PyInstaller failed ($LASTEXITCODE)" }

Copy-Item README.md, LICENSE, config.example.json dist\FunPairDL\
$zip = "dist\FunPairDL-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
# Windows' own bsdtar: a Git Bash tar earlier on PATH cannot write zips
& (Join-Path $env:SystemRoot 'System32\tar.exe') -a -c --options zip:compression=deflate -f $zip -C dist FunPairDL
if ($LASTEXITCODE) { throw "zip failed ($LASTEXITCODE)" }
"Built $zip ($([math]::Round((Get-Item $zip).Length / 1MB)) MB)"
