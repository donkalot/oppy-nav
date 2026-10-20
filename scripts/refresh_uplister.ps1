# Refreshes data/salvos_uplister.json from this PC (Salvos blocks GitHub's runners) and
# pushes it. Run monthly by the "oppy-nav Salvos snapshot" scheduled task.
$ErrorActionPreference = 'Stop'
$env:PATH = "C:\Program Files\GitHub CLI;$env:PATH"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$log = Join-Path $env:TEMP 'oppy-nav-uplister.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $git = 'C:\Program Files\Git\mingw64\bin\git.exe'
    # gh, not Git Credential Manager, which can hang waiting on a UI no task can show.
    $cred = 'credential.helper=!gh auth git-credential'
    & $git -c $cred pull -q --ff-only
    if ($LASTEXITCODE) { throw 'git pull failed' }
    & 'C:\Program Files\Python314\python.exe' scripts\scrape_all.py --refresh-uplister
    if ($LASTEXITCODE) { throw 'refresh failed' }
    & $git add data/salvos_uplister.json
    & $git diff --cached --quiet
    if ($LASTEXITCODE) {
        & $git commit -q -m 'chore: refresh Salvos uplister snapshot'
        & $git -c $cred push -q
        if ($LASTEXITCODE) { throw 'git push failed' }
    }
    Write-Output 'done'
} finally {
    Stop-Transcript | Out-Null
}
