# Detached runner for the Opus 4.8 builder session (survives harness task reaping).
Set-Location C:\Users\Brads\Python_stuff\degeneracy_v3
Remove-Item sim\ceremony\_build.done -ErrorAction SilentlyContinue
claude -p (Get-Content sim\ceremony\_prompts\build_prompt.txt -Raw) `
  --model claude-opus-4-8 `
  --permission-mode acceptEdits `
  --allowedTools "Read,Glob,Grep,Write,Edit,Bash(python*),Bash(pytest*),Bash(pip show*)" `
  | Out-File -Encoding utf8 sim\ceremony\_build_stdout.log
"done exit=$LASTEXITCODE at $(Get-Date -Format o)" | Out-File -Encoding utf8 sim\ceremony\_build.done
