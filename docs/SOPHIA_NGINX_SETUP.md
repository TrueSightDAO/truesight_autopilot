# Sophia — Nginx Setup Guide

**Sophia** (sophia.truesight.me) is the public-facing name for the TrueSight Autopilot service. This document covers the nginx reverse proxy configuration and SSL setup.

## Overview

```
Client ──HTTPS──> sophia.truesight.me:443
                        │
                   nginx (reverse proxy)
                        │
                   HTTP 127.0.0.1:8001
                        │
              FastAPI (uvicorn, 2 workers)
```

## Nginx Configuration

The config file lives at `/opt/truesight_autopilot/config/nginx/sophia.conf`.

### Install

```bash
# Create symlink to sites-available
ln -sf /opt/truesight_autopilot/config/nginx/sophia.conf /etc/nginx/sites-available/sophia

# Enable the site
ln -sf /etc/nginx/sites-available/sophia /etc/nginx/sites-enabled/

# Test and reload
nginx -t && systemctl reload nginx
```

### Key Features

| Feature | Details |
|---|---|
| **Rate limiting** | 30 req/s per IP, burst up to 50 |
| **CORS** | Pre-flight handled at nginx level; app handles actual CORS |
| **SSE support** | `/chat` endpoint has buffering disabled, long timeouts (300s) |
| **Oracle Advisory** | `/oracle-advisory` with 120s read timeout for LLM calls |
| **Security headers** | X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, Referrer-Policy |

## SSL with Certbot

```bash
# Install certbot if not present
apt-get update && apt-get install -y certbot python3-certbot-nginx

# Obtain and install certificate
certbot --nginx -d sophia.truesight.me

# Verify auto-renewal
certbot renew --dry-run
```

Certbot will modify `/etc/nginx/sites-available/sophia` to add the SSL `listen 443 ssl;` block and redirect HTTP → HTTPS.

## Endpoints

| Endpoint | Description | Timeout |
|---|---|---|
| `/` | Catch-all proxy to FastAPI | 120s |
| `/health` | Health check | 10s |
| `/chat` | SSE-streaming chat | 300s |
| `/oracle-advisory` | I Ching oracle advisory (replaces GAS bridge) | 120s |
| `/uploads/` | Uploaded file serving | 30s |
| `/static/` | Static files (7d cache) | — |

## Verifying the Setup

```bash
# Check nginx is running
systemctl status nginx

# Check the site is enabled
ls -la /etc/nginx/sites-enabled/sophia

# Test the proxy
curl -s http://localhost:8001/health
curl -s -H "Host: sophia.truesight.me" http://127.0.0.1/health

# Test SSL (after certbot)
curl -sI https://sophia.truesight.me/health

# Test rate limiting
for i in $(seq 1 100); do curl -s -o /dev/null -w "%{http_code}\n" https://sophia.truesight.me/health; done | sort | uniq -c
```

## Troubleshooting

### 502 Bad Gateway
- Ensure the FastAPI service is running: `systemctl status truesight-autopilot`
- Check port 8001 is listening: `ss -tlnp | grep 8001`

### 504 Gateway Timeout
- Increase `proxy_read_timeout` in the relevant `location` block
- Check if the LLM call is hanging

### CORS Errors
- Verify the `Access-Control-Allow-Origin` header matches the DApp origin
- The nginx pre-flight handler returns `*`; the FastAPI app handles credentialed requests

### Rate Limiting Too Aggressive
- Adjust `rate=30r/s` and `burst=50` in the `limit_req_zone` directive
- Or increase the zone size if you see `limit_req` errors in nginx logs

## Related

- [Systemd service](../systemd/truesight-autopilot.service) — runs uvicorn on port 8001
- [Oracle Advisory nginx include](../config/nginx/oracle-advisory.conf) — standalone location block for `/oracle-advisory`
- [Deployment guide](../README.md#deployment) — full EC2 setup
