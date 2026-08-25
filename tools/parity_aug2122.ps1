# Detached one-shot: chunked parity over Aug 21-22 windows.
$ErrorActionPreference = 'Continue'
$root = "C:\Users\Brads\Python_stuff\degeneracy_v3"
$log  = "$root\pilot\build\parity_chunked.log"
$done = "$root\pilot\build\parity_chunked.done"
Remove-Item $done -ErrorAction SilentlyContinue
Set-Location "$root\pilot"
python "$root\pilot\build\parity_chunked.py" *>> $log
"done $(Get-Date -Format o)" | Out-File $done -Encoding utf8
