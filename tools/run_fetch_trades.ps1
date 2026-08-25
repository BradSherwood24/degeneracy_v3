# Detached runner for the trades-tape backfill (2026-08-19).
# Per-day interleave, oldest first: the trades retention edge rolls daily, so both
# series of the oldest day are secured before moving on. Resumable (day files skip).
# Writes historical-data\trades_fetch.done when complete; log alongside it.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\historical-data\trades_fetch.log"
$done = "$root\historical-data\trades_fetch.done"
Remove-Item $done -ErrorAction SilentlyContinue

$d   = [datetime]::ParseExact('2026-06-11','yyyy-MM-dd',$null)
$end = (Get-Date).ToUniversalTime().Date.AddDays(-1)
while ($d -le $end) {
    $ds = $d.ToString('yyyy-MM-dd')
    python "$root\tools\fetch_history.py" --stage trades15 --start $ds --end $ds *>> $log
    python "$root\tools\fetch_history.py" --stage trades1h --start $ds --end $ds *>> $log
    $d = $d.AddDays(1)
}
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
