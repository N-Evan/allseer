# Start llama-server for allseer on :8081.
#   powershell -ExecutionPolicy Bypass -File .\llama.ps1
# Ports: 8077 allseer, 8080 SearXNG (Docker), 8081 llama-server.
# Edit $exe and $model below to match your machine.

$exe   = "$PSScriptRoot\llama.cpp\llama-server.exe"
$model = "$PSScriptRoot\models\Qwen3-30B-A3B-Instruct-2507-UD-Q4_K_XL.gguf"
$url   = 'https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF/resolve/main/Qwen3-30B-A3B-Instruct-2507-UD-Q4_K_XL.gguf'

# llama-server's own -hf downloader stalled near the end of a 400MB test file, so the
# model is fetched with curl instead - it resumes, -hf does not.
if (-not (Test-Path $model)) {
    "Model missing. Downloading 17.7GB (resumable - rerun this script if it drops):"
    curl.exe -L -C - --retry 5 -o $model $url
    if ($LASTEXITCODE -ne 0) { "download failed, rerun to resume"; exit 1 }
}

# Expert layers parked in system RAM. This model is 48 layers and 17.7GB, which does not
# fit 16GB of VRAM - but only 3B of its 30B params are active per token, so experts left
# in RAM cost far less speed than their size suggests. Lower until load OOMs, then +2.
# The "load_tensors: CPU_Mapped model buffer size" line shows the split you actually got.
$cpuMoe = 14

& $exe -m $model `
  --n-cpu-moe $cpuMoe `
  -ngl 99 `
  -c 16384 `
  --reasoning-budget 0 `
  --cache-type-k q8_0 --cache-type-v q8_0 `
  --host 127.0.0.1 --port 8081
