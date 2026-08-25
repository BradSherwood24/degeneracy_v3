# Detached one-shot: tape_sim over 2026-08-21..22 for the pilot parity check.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\sim\out\sim_aug2122.log"
$done = "$root\sim\out\sim_aug2122.done"
Remove-Item $done -ErrorAction SilentlyContinue
Set-Location "$root\sim"
python tape_sim.py --start 2026-08-21 --end 2026-08-22 --out out\day0821_22 --census out\census_train.csv *>> $log
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
