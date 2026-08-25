# Rung 1.5 frozen week run (2026-08-19): June 13-19, both directions, dual staleness.
# Detached (harness-tracked tasks get killed on this box). Writes week_tape.done when finished.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\sim\out\week_tape.log"
$done = "$root\sim\out\week_tape.done"
Remove-Item $done -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$root\sim\out\week60" | Out-Null
New-Item -ItemType Directory -Force "$root\sim\out\week5"  | Out-Null

"=== week60 start $(Get-Date -Format o) ===" | Out-File $log -Encoding utf8
python "$root\sim\tape_sim.py" --start 2026-06-13 --end 2026-06-19 --out "$root\sim\out\week60" --staleness 60 *>> $log
"=== week60 exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"=== week5 start $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
python "$root\sim\tape_sim.py" --start 2026-06-13 --end 2026-06-19 --out "$root\sim\out\week5" --staleness 5 *>> $log
"=== week5 exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
