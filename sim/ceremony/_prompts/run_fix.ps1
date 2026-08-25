# Detached runner for the Opus 4.8 fix session.
Set-Location C:\Users\Brads\Python_stuff\degeneracy_v3
Remove-Item sim\ceremony\_fix.done -ErrorAction SilentlyContinue
claude -p (Get-Content sim\ceremony\_prompts\fix_prompt.txt -Raw) `
  --model claude-opus-4-8 `
  --permission-mode acceptEdits `
  --allowedTools "Read,Glob,Grep,Write,Edit,Bash(python*),Bash(pytest*)" `
  | Out-File -Encoding utf8 sim\ceremony\_fix_stdout.log
"done exit=$LASTEXITCODE at $(Get-Date -Format o)" | Out-File -Encoding utf8 sim\ceremony\_fix.done
