# THE ONE-SHOT SEALED READ — Rung 1.5 (2026-08-20, Brad's go: "break the seal. freeze it and run.")
# Detached so nothing can kill it mid-read (a crash spends the seal). Runs exactly once.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\sim\out\unseal15_run.log"
"=== SEALED READ start $(Get-Date -Format o) ===" | Out-File $log -Encoding utf8
python "$root\sim\unseal_runner15.py" --i-have-brads-explicit-go *>> $log
"=== SEALED READ exit=$LASTEXITCODE $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
