$target = "$env:TEMP\OllamaSetup.exe"
$url = "https://ollama.com/download/OllamaSetup.exe"
Write-Host "Target: $target"
do {
    Write-Host "Resuming Ollama download..."
    & curl.exe -C - -L -o $target $url
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Connection reset or interrupted. Auto-retrying in 5 seconds..."
        Start-Sleep -Seconds 5
    }
} while ($LASTEXITCODE -ne 0)
Write-Host "Ollama installer download completed successfully!"
