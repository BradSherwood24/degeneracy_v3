# One-shot detached runner: trades tape for 2026-08-21..22 (parity refetch).
# Detached from the Claude session so background-task reaping can't touch it.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\historical-data\fetch_aug2122.log"
$done = "$root\historical-data\fetch_aug2122.done"
Remove-Item $done -ErrorAction SilentlyContinue
python "$root\tools\fetch_history.py" --stage trades15 --start 2026-08-21 --end 2026-08-22 *>> $log
python "$root\tools\fetch_history.py" --stage trades1h --start 2026-08-21 --end 2026-08-22 *>> $log
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
