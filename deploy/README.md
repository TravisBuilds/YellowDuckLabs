# Deploy Fire Watch to yellowducklabs.org

GoDaddy holds the domain. A Linux VPS runs Docker. Caddy issues the certificate.

## 1. VPS

Create an Ubuntu 24.04 droplet (2 GB RAM is enough) and install Docker.

**Oracle Cloud Always Free** works if you create **one** `VM.Standard.A1.Flex` (Ampere, ARM): **2 OCPU / 12 GB**, Ubuntu 24.04, public IPv4, boot volume ~50–80 GB. That is enough for this stack. Do **not** use the AMD `E2.1.Micro` (1 GB) — PostGIS, the API and Next will not fit.

In the VCN security list (or NSG) allow TCP **80** and **443** from `0.0.0.0/0`. Oracle images also filter in `iptables`; if the site is unreachable after Caddy is up, open those ports on the instance too:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save   # if the package is installed
```

Free ARM capacity is often exhausted in popular regions. If create-instance fails, retry another availability domain or a quieter home region (home region is permanent). Images here are multi-arch; ARM is fine.

```bash
curl -fsSL https://get.docker.com | sh
```

Open ports **80** and **443**. Do not publish 5432.

## 2. Copy the project and boot

On the VPS:

```bash
git clone <this-repo> FireWatch && cd FireWatch
cp .env.production.example .env.production
# set POSTGRES_PASSWORD to a long random string (same value in DATABASE_URL)
chmod +x deploy/bootstrap.sh
./deploy/bootstrap.sh            # empty schema
# or, after copying a dump from your laptop:
# ./deploy/bootstrap.sh deploy/firewatch.dump
```

From this laptop, to snapshot the ingested municipalities:

```bash
./deploy/dump-local.sh
# scp deploy/firewatch.dump user@VPS_IP:FireWatch/deploy/
```

## 3. GoDaddy DNS

DNS → yellowducklabs.org → Manage DNS. Do not use GoDaddy Website / cPanel hosting for this app.

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `@` | *VPS public IPv4* | 600 |
| A | `www` | *VPS public IPv4* | 600 |

Leave MX records alone if you use GoDaddy email. After DNS settles, Caddy obtains the cert automatically. Open https://yellowducklabs.org.

## 4. NASA FIRMS key (recommended)

Near-real-time satellite detections need a free MAP key:

1. Open https://firms.modaps.eosdis.nasa.gov/api/map_key/
2. Enter `firewatch@yellowducklabs.org` (or any inbox you monitor).
3. Paste the key into `.env.production` on the VPS:

```bash
FIRMS_MAP_KEY=your-key-here
```

4. Restart the API and ingest once:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d api
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T api \
  python -m firewatch ingest -m west-vancouver --skip-boundary --only nasa_firms
```

Without a key, `nasa_firms` stays `UNAVAILABLE` (not zero detections).

## 5. Daily refresh

Live weather and hotspot sources should be re-ingested daily. On the VPS:

```bash
chmod +x deploy/daily-refresh.sh deploy/install-daily-refresh.sh
./deploy/install-daily-refresh.sh
```

This installs a cron job at **06:00 America/Vancouver** (13:00 UTC during PDT). Logs go to `/var/log/firewatch-refresh.log`.

To run manually:

```bash
./deploy/daily-refresh.sh
tail -50 /var/log/firewatch-refresh.log
```
