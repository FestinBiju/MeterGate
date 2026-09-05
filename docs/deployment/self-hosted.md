# Self-hosted Docker bundle

This bundle packages MeterGate as Linux containers for an always-on Ubuntu host or VM. It is a staging/Test Mode deployment. It does not package secrets, database data, passkeys, payment credentials, or Cloudflare credentials.

## Build the transferable image archive

From a Windows development machine with Docker Desktop running:

```powershell
./scripts/package-self-hosted.ps1
```

The command creates `dist/self-hosted` containing the Compose file, Caddy configuration, a secret template, image archive, and SHA-256 checksum. It builds the web image with `https://staging.metergate.tech` as its public API origin. Supply another public origin before packaging only when the deployment domain changes:

```powershell
./scripts/package-self-hosted.ps1 -PublicOrigin https://staging.example.com
```

Copy the entire `dist/self-hosted` directory to the Ubuntu host. Verify its checksum before loading images.

## Configure the host

Install Docker Engine and the Compose plugin. On the host, copy `.env.self-hosted.example` to `.env.self-hosted`, generate independent secrets, and restrict it to the host administrator:

```bash
cp .env.self-hosted.example .env.self-hosted
chmod 600 .env.self-hosted
sha256sum -c SHA256SUMS.txt
docker load --input metergate-self-hosted-images.tar
```

Create a Cloudflare Tunnel with the public hostname `staging.metergate.tech` targeting `http://caddy:80`, then place its token only in `.env.self-hosted`. Cloudflare terminates public TLS and no Docker port is published to the host.

Before ordinary startup, initialize the durable database exactly once:

```bash
docker compose --env-file .env.self-hosted --profile bootstrap run --rm bootstrap
docker compose --env-file .env.self-hosted up -d
```

Set Razorpay Test Mode's webhook to `https://staging.metergate.tech/api/v1/webhooks/razorpay` with the exact dedicated webhook secret in `.env.self-hosted`.

## Operational boundaries

- Do not place `.env.self-hosted`, the image archive, database dumps, or Cloudflare tokens in Git.
- PostgreSQL and Redis expose no host ports. Do not add host ports for them.
- Keep `RAZORPAY_MODE=test`; Live Mode is not supported by this milestone.
- Back up PostgreSQL before upgrading images. Test one restore before using the deployment for a demo.
- The tunnel must run continuously; when it is stopped the public hostname is unavailable, but private state remains on the host.
