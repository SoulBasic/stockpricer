# Deploying behind a reverse proxy (public internet)

The server speaks plain HTTP and is meant to sit **behind a TLS-terminating
reverse proxy**. Don't publish port 8849 to the internet directly.

Recommended topology:

```
client ──HTTPS──> reverse proxy (Caddy/nginx) ──HTTP──> 127.0.0.1:8849 (stockpricer)
```

## Steps

1. **Run the app on loopback.** The compose file defaults to
   `BIND_ADDR=127.0.0.1`, so the container is only reachable from the host
   (where the proxy runs). Set `TRUST_PROXY=1` so per-client rate limiting
   sees the real client IP from `X-Real-IP` / `X-Forwarded-For`:

   ```bash
   echo "TRUST_PROXY=1" >> .env
   docker compose up -d
   ```

2. **Put a proxy in front** — pick one:
   - **Caddy** (`Caddyfile`) — automatic HTTPS, simplest. Edit the domain, then
     `caddy run --config ./Caddyfile`.
   - **nginx** (`nginx.conf`) — copy to `/etc/nginx/conf.d/`, get certs with
     `certbot --nginx -d your.domain`, reload nginx. Includes `limit_req` +
     connection limits + tight timeouts.

3. **Firewall.** Allow only 80/443 to the proxy; do not expose 8849.

## What protects you

| Layer | Control |
|------|---------|
| Reverse proxy | TLS, request size cap, client/read timeouts, edge rate/connection limits |
| App (`stock_server.py`) | per-IP token-bucket rate limit (`RATE_LIMIT_RPM`), client socket timeout (`SOCKET_TIMEOUT`), bounded upstream reads (`MAX_RESPONSE_BYTES`), bounded resolve cache (`RESOLVE_CACHE_MAX`) |
| Container (`docker-compose.yml`) | non-root, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, `mem_limit`, `pids_limit` |

Tune knobs via `.env` — see `../.env.example`. For this service's traffic the
defaults are already very generous; lower `RATE_LIMIT_RPM` / nginx `rate=` if
you want a tighter cap.
