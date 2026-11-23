# Cloud backend

Install dependencies with `python -m pip install -r requirements.txt`, copy
`.env.example` to `.env`, then set `VSM_CORS_ORIGINS` to the dashboard's exact
origin(s), separated by commas. Keep `.env` private.

The default SQLite file is `vsm.db` next to `main.py`.
The login limiter allows 5 attempts per minute per client IP; it is in-memory
and applies per backend process.

Create a user with `python main.py add-user <username> <password>`. Run Uvicorn
on loopback so only the local HTTPS proxy can reach it:

```sh
uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

Run backend tests with `python -m pip install -r requirements-dev.txt` and
`python -m pytest`.

## Public HTTPS with Caddy

Point a public DNS name (for example `monitor.example.com`) at the server, then
use this Caddyfile:

```caddy
monitor.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy obtains and renews a Let's Encrypt certificate automatically when the
domain is publicly reachable on ports 80 and 443. Set the gateway's `VSM_URL`
to `https://monitor.example.com`.

## HTTPS on a LAN

For a local hostname such as `pi.local`, Caddy can issue a private certificate:

```caddy
https://pi.local {
    tls internal
    reverse_proxy 127.0.0.1:8000
}
```

Install Caddy's local root CA certificate on the gateway (or point
`REQUESTS_CA_BUNDLE` at that certificate). Keep certificate verification on;
do not use `verify=False`.

## Docker

The backend image uses Python 3.11, runs as a non-root user, and stores the
database under `/data`. From the repository root:

```sh
docker build -t vsm-cloud ./cloud
docker volume create vsm-data
docker run -d --name vsm-cloud --restart unless-stopped \
  --env-file ./cloud/.env \
  -p 127.0.0.1:8000:8000 \
  -v vsm-data:/data \
  vsm-cloud
```

The localhost-only port mapping keeps the API behind the HTTPS proxy. Create
users with `docker exec -it vsm-cloud python main.py add-user <username> <password>`.