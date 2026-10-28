[CmdletBinding()]
param(
	[string[]]$HiddenImports = @()
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot
try {
	if (-not (Test-Path '.venv\Scripts\python.exe')) {
		py -3.11 -m venv .venv
		if ($LASTEXITCODE -ne 0) { throw 'Creating the Python 3.11 virtual environment failed.' }
	}
	$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
	& $Python -m pip install -e '.[dev,package]'
	if ($LASTEXITCODE -ne 0) { throw 'Installing build dependencies failed.' }
	$BuildArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir', '--name', 'GmailInboxCleaner', '--paths', '.', '--collect-all', 'keyring', '--collect-all', 'tzdata', '--hidden-import', 'gmail_clean.demo_ai')
	foreach ($Module in $HiddenImports) { $BuildArgs += @('--hidden-import', $Module) }
	$BuildArgs += 'scripts/frozen_entry.py'
	& $Python @BuildArgs
	if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
	Write-Output 'Application: dist\GmailInboxCleaner\GmailInboxCleaner.exe'
	Write-Output 'Smoke-test demo and configured AI dependencies on the target Windows PC before use.'
}
finally { Pop-Location }
