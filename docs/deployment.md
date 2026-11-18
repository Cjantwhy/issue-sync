# Deployment

issue-sync runs as one Docker Compose project on a single host: `postgres`, `redis`, a
one-shot `migrate`, `api` and `worker`. Google (Apps Script) and Jira must be able to
call its webhooks, and requesters open the detail pages, so it needs one public HTTPS
entry point that exposes only those paths. Choose one:

- **Option A: Cloudflare Tunnel** (recommended). A `cloudflared` container dials out to
  Cloudflare, so the host opens **no inbound port** and its IP stays private. Cloudflare
  provides TLS, the WAF and path routing. This works the same on a VPS, on a machine
  behind NAT or on a laptop. It requires the domain's DNS to be on Cloudflare.
- **Option B: a reverse proxy on the host** (Caddy, Nginx, ...). This is the usual
  setup for a server with a public IP and any DNS provider. The proxy terminates TLS on
  port 443 and forwards the public paths to `127.0.0.1:8000`.

```
A: Internet ──► Cloudflare (WAF) ──► Tunnel ──► cloudflared ──► api:8000
B: Internet ──► :443 Caddy (TLS, path allow-list) ──► 127.0.0.1:8000 ──► api
```

Without either, the system still works end to end: the worker polls the sheets every
minute and reconciliation brings Jira's changes back every 15 minutes. Only the instant
webhooks and public detail links need the entry point. To try it on a laptop, the plain
`docker compose up -d` from the README is enough.

## 1. Host

Any Linux host with Docker Engine and Compose v2.24 or newer (the production overlay uses
`!reset`). 1 vCPU and 1 GB of RAM are enough for a few thousand requests a month; the
LLM and Jira calls are the slow part, not the host.

```bash
git clone <your fork> issue-sync && cd issue-sync
cp .env.example .env                      # then fill it in, see the README
for f in jira slack classifier assignment; do cp config/$f.example.yaml config/$f.yaml; done
mkdir -p secrets                          # put google-service-account.json here
chmod 600 .env secrets/*
```

Use strong values for `POSTGRES_PASSWORD`, `SHEETS_WEBHOOK_SECRET`,
`JIRA_WEBHOOK_SECRET` and `DASHBOARD_PASSWORD` (for example `openssl rand -base64 32`),
and set `PUBLIC_BASE_URL` to the tunnel's public hostname, e.g.
`https://requests.example.com`. Detail links written to the sheet and to Jira use it.

## 2A. Cloudflare Tunnel

The domain must use Cloudflare DNS.

1. Zero Trust > Networks > Tunnels > **Create a tunnel** (type *Cloudflared*). Copy the
   token from the install command into `.env` as `TUNNEL_TOKEN`.
2. Add **public hostnames** for the hostname in `PUBLIC_BASE_URL`, all pointing at
   service `http://api:8000` (the Compose service name, not localhost):

   | Path (regex) | Why it is public |
   |---|---|
   | `^/webhooks/(sheets\|jira)$` | Apps Script and Jira deliver events here |
   | `^/r/` | Detail pages linked from the sheet, Jira and Slack |
   | `^/static/` | Stylesheet and icon of the detail pages |

   Add no catch-all route. Every other path (`/dashboard`, `/health`, API docs) then
   gets Cloudflare's 404 and never reaches the app.
3. Security > WAF > **Custom rules**, action *Block*, one rule:

   ```
   (http.host eq "requests.example.com" and (
     (http.request.uri.path in {"/webhooks/sheets" "/webhooks/jira"} and http.request.method ne "POST")
     or (starts_with(http.request.uri.path, "/r/") and http.request.method ne "GET")
     or (starts_with(http.request.uri.path, "/static/") and http.request.method ne "GET")
   ))
   ```

   Write the conditions as "block when the method is **not** the expected one". The
   inverted form blocks every webhook delivery.
4. Security > Bots: turn **Bot Fight Mode off** for the zone. Jira's deliveries come from
   Atlassian's servers and look like bots. Bot Fight Mode answers them with a challenge,
   and Jira does not retry those deliveries. (On the free plan it cannot be turned off
   for one path.)

The application authenticates every webhook itself: Jira bodies are HMAC-signed, Apps
Script sends a shared secret. The tunnel narrows what is reachable, but security does not
depend on the hostname staying secret.

Start with the `tunnel` profile:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tunnel up -d --build --wait
```

## 2B. Reverse proxy (Caddy)

Point a DNS record at the server, open ports 80 and 443 (Caddy obtains the certificate
over port 80), and keep every other port closed. With
[Caddy](https://caddyserver.com/docs/install) installed on the host, the whole
configuration is `/etc/caddy/Caddyfile`:

```caddyfile
requests.example.com {
	@public {
		path /r/* /static/*
		method GET HEAD
	}
	@webhooks {
		path /webhooks/sheets /webhooks/jira
		method POST
	}
	handle @public {
		reverse_proxy 127.0.0.1:8000
	}
	handle @webhooks {
		request_body {
			max_size 2MB
		}
		reverse_proxy 127.0.0.1:8000
	}
	handle {
		respond 404
	}
}
```

Like the tunnel's routes, this allow-list keeps `/dashboard`, `/health` and everything
else off the internet. Start without the profile; `TUNNEL_TOKEN` is not needed:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait
sudo systemctl reload caddy
```

## 3. Check and register

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -s localhost:8000/health        # on the host: {"status":"ok",...}
```

`migrate` runs the database migrations and exits; `api` and `worker` start only after
it succeeds, and both refuse to start if a configuration file does not cover the
database's constants (the error names what is missing). Then:

```bash
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tunnel'
# (leave out --profile tunnel with option B)
dc exec worker issue-sync jira-check                       # credentials, fields, statuses
dc exec worker issue-sync add-document --name "Support requests" \
  --spreadsheet "https://docs.google.com/spreadsheets/d/<id>/edit" --worksheet "Requests"
```

Finally register the Jira webhook and the Apps Script trigger (README, "Jira webhook"
and "Google Sheets setup"), pointing them at `https://<public host>/webhooks/...`.

The dashboard is not published. Open it through an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 you@host     # then browse http://localhost:8000/dashboard
```

## 4. Operating it

| Task | Command |
|---|---|
| Logs | `dc logs -f worker` (rotated: 3 × 10 MB per service) |
| Upgrade | `git pull && dc up -d --build --wait` (migrations run automatically) |
| Reconcile now | `dc exec worker issue-sync reconcile` |
| Back up | `dc exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > backup-$(date +%F).dump` |
| Restore | `dc exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean < backup.dump` |

A daily backup from cron, kept for two weeks:

```cron
30 2 * * * cd /srv/issue-sync && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > backups/$(date +\%F).dump && find backups -name '*.dump' -mtime +14 -delete
```

Redis holds only the job queue, retry state and short-lived marks (alert de-duplication,
new-row settling). It persists with AOF, and losing it loses no data: the next poll and
reconciliation rebuild everything from the sheet, the database and Jira.

Problems that need a person (a push that keeps failing, a ticket deleted in Jira, a
duplicated row identity, a worksheet that suddenly lost most rows) are posted to the
Slack alert channel by the reconciliation job, at most once a day each.
