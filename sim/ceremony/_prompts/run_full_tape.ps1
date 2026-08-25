# Rung 1.5 full-tape run (2026-08-19): all train days 06-13..08-01, both staleness passes.
# Brad-sanctioned; preregistered analyses in claudes-corner/rung15_findings_2026_08_19.md.
# TRAIN ONLY — loader refuses sealed dates regardless (tested).
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\sim\out\full_tape.log"
$done = "$root\sim\out\full_tape.done"
Remove-Item $done -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$root\sim\out\full60" | Out-Null
New-Item -ItemType Directory -Force "$root\sim\out\full5"  | Out-Null

"=== full60 start $(Get-Date -Format o) ===" | Out-File $log -Encoding utf8
python "$root\sim\tape_sim.py" --start 2026-06-13 --end 2026-08-01 --out "$root\sim\out\full60" --staleness 60 *>> $log
"=== full60 exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"=== full5 start $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
python "$root\sim\tape_sim.py" --start 2026-06-13 --end 2026-08-01 --out "$root\sim\out\full5" --staleness 5 *>> $log
"=== full5 exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8

"=== viz aggregate start $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
Copy-Item "$root\claudes-corner\viz\data.js" "$root\claudes-corner\viz\data_week.js" -Force
python "$root\claudes-corner\viz\aggregate.py" --src60 "$root\sim\out\full60\tape_points.csv" --src5 "$root\sim\out\full5\tape_points.csv" --label "ALL train days 2026-06-13..08-01 (full60/full5)" *>> $log
"=== viz aggregate exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
