param(
  [string]$OutputDirectory = "dist/self-hosted",
  [string]$ImageTag = "self-hosted",
  [string]$PublicOrigin = "https://staging.metergate.tech"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$composePath = Join-Path $repositoryRoot "infrastructure/self-hosted/compose.yml"
$outputPath = Join-Path $repositoryRoot $OutputDirectory
$environmentPath = Join-Path $env:TEMP "metergate-package-$PID.env"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker is required to package MeterGate."
}

try {
  @"
METERGATE_IMAGE_TAG=$ImageTag
METERGATE_PLATFORM=linux/amd64
METERGATE_RUNTIME_ENV_FILE=$environmentPath
PUBLIC_ORIGIN=$PublicOrigin
CLOUDFLARE_TUNNEL_TOKEN=package-placeholder
POSTGRES_DB=metergate
POSTGRES_USER=postgres
POSTGRES_PASSWORD=package-placeholder
REDIS_URL=redis://redis:6379/0
APP_ENV=staging
ORBITINTEL_ENVIRONMENT=test
ORBITINTEL_SHARED_SECRET=package-placeholder
"@ | Set-Content -LiteralPath $environmentPath -NoNewline

  $composeArguments = @("compose", "--env-file", $environmentPath, "-f", $composePath)
  $runtimeImages = @(
    "postgres:17-alpine",
    "redis:7.4-alpine",
    "caddy:2-alpine",
    "cloudflare/cloudflared:latest"
  )
  foreach ($runtimeImage in $runtimeImages) {
    & docker image pull $runtimeImage
    if ($LASTEXITCODE -ne 0) { throw "Unable to pull runtime images." }
  }

  & docker @composeArguments build
  if ($LASTEXITCODE -ne 0) { throw "Unable to build MeterGate images." }

  New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
  $images = @(
    "metergate/api:$ImageTag",
    "metergate/orbitintel:$ImageTag",
    "metergate/web:$ImageTag",
    "postgres:17-alpine",
    "redis:7.4-alpine",
    "caddy:2-alpine",
    "cloudflare/cloudflared:latest"
  )
  & docker image save --output (Join-Path $outputPath "metergate-self-hosted-images.tar") $images
  if ($LASTEXITCODE -ne 0) { throw "Unable to create the image archive." }

  Copy-Item -LiteralPath $composePath -Destination (Join-Path $outputPath "compose.yml") -Force
  Copy-Item -LiteralPath (Join-Path $repositoryRoot "infrastructure/self-hosted/Caddyfile") -Destination (Join-Path $outputPath "Caddyfile") -Force
  Copy-Item -LiteralPath (Join-Path $repositoryRoot "infrastructure/self-hosted/.env.self-hosted.example") -Destination (Join-Path $outputPath ".env.self-hosted.example") -Force
  $imageArchive = Join-Path $outputPath "metergate-self-hosted-images.tar"
  $sha256 = (Get-FileHash -Algorithm SHA256 $imageArchive).Hash.ToLowerInvariant()
  "$sha256  metergate-self-hosted-images.tar" | Set-Content -LiteralPath (Join-Path $outputPath "SHA256SUMS.txt") -NoNewline
} finally {
  Remove-Item -LiteralPath $environmentPath -Force -ErrorAction SilentlyContinue
}
