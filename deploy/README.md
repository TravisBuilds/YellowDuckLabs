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
