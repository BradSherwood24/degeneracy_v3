# Detached runner for BOTH adversarial review sessions (sequential to limit load).
Set-Location C:\Users\Brads\Python_stuff\degeneracy_v3
Remove-Item sim\ceremony\_reviews.done -ErrorAction SilentlyContinue

claude -p (Get-Content sim\ceremony\_prompts\adversarial_review_A_prompt.txt -Raw) `
  --model claude-opus-4-8 `
  --permission-mode acceptEdits `
  --allowedTools "Read,Glob,Grep,Write,Edit,Bash(python*),Bash(pytest*)" `
  | Out-File -Encoding utf8 sim\ceremony\rung1_adversarial_review_A.md
"A done exit=$LASTEXITCODE at $(Get-Date -Format o)" | Out-File -Encoding utf8 -Append sim\ceremony\_reviews.progress

claude -p (Get-Content sim\ceremony\_prompts\adversarial_review_B_prompt.txt -Raw) `
  --model claude-opus-4-8 `
  --permission-mode acceptEdits `
  --allowedTools "Read,Glob,Grep,Write,Edit,Bash(python*),Bash(pytest*)" `
  | Out-File -Encoding utf8 sim\ceremony\rung1_adversarial_review_B.md
"B done exit=$LASTEXITCODE at $(Get-Date -Format o)" | Out-File -Encoding utf8 -Append sim\ceremony\_reviews.progress

"done at $(Get-Date -Format o)" | Out-File -Encoding utf8 sim\ceremony\_reviews.done
