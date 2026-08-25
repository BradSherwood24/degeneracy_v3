# Rung 1.5 full-tape run, CHUNKED (2026-08-19): the monolithic 50-day run MemoryErrors
# (tape_sim loads all trades up front; week-size is the proven-fit configuration).
# Week-sized chunks are law-equivalent: windows partition by _assigned_day (tape_sim.py:419).
# Chunk receipts/reports live in full60_chunks/ and full5_chunks/; the concatenated
# tape_points.csv lands in full60/ and full5/ for the viz aggregator.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\sim\out\full_tape.log"
$done = "$root\sim\out\full_tape.done"
Remove-Item $done -ErrorAction SilentlyContinue

$chunks = @(
  @("2026-06-13","2026-06-19"), @("2026-06-20","2026-06-26"),
  @("2026-06-27","2026-07-03"), @("2026-07-04","2026-07-10"),
  @("2026-07-11","2026-07-17"), @("2026-07-18","2026-07-24"),
  @("2026-07-25","2026-07-31"), @("2026-08-01","2026-08-01")
)

"=== chunked full-tape start $(Get-Date -Format o) ===" | Out-File $log -Encoding utf8
foreach ($s in @(60, 5)) {
  $croot = "$root\sim\out\full${s}_chunks"
  for ($i = 0; $i -lt $chunks.Count; $i++) {
    $a = $chunks[$i][0]; $b = $chunks[$i][1]
    $outdir = "$croot\c$i"
    if (Test-Path "$outdir\tape_points.csv") {
      "=== s=$s c$i ($a..$b) already done, skipping ===" | Out-File $log -Append -Encoding utf8
      continue
    }
    New-Item -ItemType Directory -Force $outdir | Out-Null
    "=== s=$s c$i ($a..$b) start $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
    python "$root\sim\tape_sim.py" --start $a --end $b --out $outdir --staleness $s *>> $log
    "=== s=$s c$i exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { "FATAL chunk failure s=$s c$i" | Out-File $log -Append -Encoding utf8 }
  }
  python "$root\claudes-corner\viz\concat_chunks.py" --chunks-root $croot --out "$root\sim\out\full$s\tape_points.csv" *>> $log
  "=== concat s=$s exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
}

"=== viz aggregate start $(Get-Date -Format o) ===" | Out-File $log -Append -Encoding utf8
if (-not (Test-Path "$root\claudes-corner\viz\data_week.js")) {
  Copy-Item "$root\claudes-corner\viz\data.js" "$root\claudes-corner\viz\data_week.js" -Force
}
python "$root\claudes-corner\viz\aggregate.py" --src60 "$root\sim\out\full60\tape_points.csv" --src5 "$root\sim\out\full5\tape_points.csv" --label "ALL train days 2026-06-13..08-01 (full60/full5, chunked)" *>> $log
"=== viz aggregate exit=$LASTEXITCODE ===" | Out-File $log -Append -Encoding utf8
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
