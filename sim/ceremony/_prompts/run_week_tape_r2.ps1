# Rung 1.5 week rerun #2 (2026-08-19): post-A15.9/A15.10 (cross-side refutation + honest dwell).
# June 13-19, both directions, dual staleness. Original pre-refutation run preserved in week60/week5.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\sim\out\week_tape_r2.log"
$done = "$root\sim\out\week_tape_r2.done"
Remove-Item $done -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$root\sim\out\week60_r2" | Out-Null
New-Item -ItemType Directory -Force "$root\sim\out\week5_r2"  | Out-Null

"=== week60_r2 start $(Get-Date -Format o) ===" | Out-File $log -Encoding utf8
python "$root\sim\tape_sim.py" --start 2026-06-13 --end 2026-06-19 --out "$root\sim\out\week60_r2" --staleness 60 *>> $log
"=== week60_r2 exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"=== week5_r2 start $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
python "$root\sim\tape_sim.py" --start 2026-06-13 --end 2026-06-19 --out "$root\sim\out\week5_r2" --staleness 5 *>> $log
"=== week5_r2 exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
