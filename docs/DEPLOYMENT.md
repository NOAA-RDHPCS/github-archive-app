# GitHub Archive System - Deployment Guide

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Architecture](#architecture)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Deployment Options](#deployment-options)
7. [GitHub Webhook Setup](#github-webhook-setup)
8. [Verification](#verification)
9. [Operations](#operations)
10. [Troubleshooting](#troubleshooting)
11. [Security Considerations](#security-considerations)

---

## Overview

The GitHub Archive System is a webhook-based archival solution that captures all non-git activity from GitHub organizations. It provides:

- **Immutable storage** of all issues, pull requests, comments, reviews, discussions, releases, and more
- **Complete version history** - every edit is preserved
- **Cryptographic integrity verification** via SHA-256 checksums
- **NARA-compliant exports** for federal records management
- **High availability** option with PostgreSQL replication

### What Gets Captured

| Content Type | Data Captured |
|--------------|---------------|
| Issues | Title, body, labels, assignees, state changes |
| Issue Comments | Full comment text, author, timestamps |
| Pull Requests | Title, body, reviewers, merge status |
| PR Reviews | Review body, approval state |
| PR Review Comments | Inline comments with file/line context |
| Discussions | Title, body, category, answers |
| Discussion Comments | Full replies |
| Releases | Release notes, assets |
| Milestones | Title, description, due dates |
| Wiki Pages | Page content summaries |
| Projects | Board metadata, items |

---

## Prerequisites

### Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 2 cores | 4+ cores |
| RAM | 4 GB | 8+ GB |
| Storage | 50 GB SSD | 200+ GB SSD |
| Network | 100 Mbps | 1 Gbps |

### Software Requirements

- **Operating System**: RHEL 8+, Ubuntu 20.04+, or similar Linux
- **Container Runtime**: Docker 20.10+ or Podman 4.0+
- **Docker Compose**: v2.0+ (or podman-compose)

### Network Requirements

- HTTPS access from GitHub webhook IPs (see [GitHub Meta API](https://api.github.com/meta))
- Outbound HTTPS to `api.github.com` (for IP list updates)
- Internal network access to PostgreSQL (if external DB)

---

## Architecture

### Single Server Deployment

```
                    Internet
                        │
                        ▼
              ┌─────────────────┐
              │   Firewall      │
              │  (IP allowlist) │
              └────────┬────────┘
                       │ :8443
                       ▼
┌──────────────────────────────────────────────────────┐
│                  Docker Network                       │
│                                                       │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────┐ │
│  │   Traefik   │──▶│   Archive    │──▶│ PostgreSQL│ │
│  │   (TLS +    │   │   Service    │   │ (immutable│ │
│  │  IP filter) │   │  (FastAPI)   │   │  storage) │ │
│  └─────────────┘   └──────────────┘   └───────────┘ │
│                            │                         │
│                            ▼                         │
│                    ┌──────────────┐                  │
│                    │  JSON Files  │                  │
│                    │   (backup)   │                  │
│                    └──────────────┘                  │
└──────────────────────────────────────────────────────┘
```

### High Availability Deployment

```
                    Internet
                        │
                        ▼
              ┌─────────────────┐
              │  Load Balancer  │
              └────────┬────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
    ┌─────────┐   ┌─────────┐   ┌─────────┐
    │ Node 1  │   │ Node 2  │   │ Node 3  │
    │ Archive │   │ Archive │   │ Archive │
    └────┬────┘   └────┬────┘   └────┬────┘
         │             │             │
         └─────────────┼─────────────┘
                       ▼
              ┌─────────────────┐
              │   PostgreSQL    │
              │    Primary      │
              └────────┬────────┘
                       │ replication
                       ▼
              ┌─────────────────┐
              │   PostgreSQL    │
              │    Replica      │
              └─────────────────┘
```

---

## Installation

### Step 1: Clone/Copy the Archive System

```bash
# Copy the github-archive directory to your server
scp -r github-archive/ user@server:/opt/

# Or clone from your internal git repository
git clone https://internal-git/github-archive.git /opt/github-archive
```

### Step 2: Create Data Directories

```bash
sudo mkdir -p /var/lib/github-archive/{json,postgres,backups}
sudo chown -R 1000:1000 /var/lib/github-archive
```

### Step 3: Run Setup Script

```bash
cd /opt/github-archive
chmod +x scripts/*.sh
./scripts/setup.sh
```

The setup script will:
- Check for required tools (docker, openssl)
- Create necessary directories
- Generate self-signed TLS certificates
- Create `.env` file with random secrets
- Fetch initial GitHub webhook IPs
- Build Docker images

### Step 4: Configure Environment

```bash
nano .env
```

Required settings to configure:

```bash
# Your GitHub organizations (comma-separated)
ALLOWED_ORGS=NOAA-GFDL,NOAA-RDHPCS

# Your server's domain name or IP
DOMAIN=github-archive.example.gov

# TLS mode (selfsigned, provided, or acme)
TLS_MODE=provided  # Use 'provided' for production with real certs

# Storage paths (already set by setup, verify they're correct)
ARCHIVE_PATH=/var/lib/github-archive/json
POSTGRES_DATA_PATH=/var/lib/github-archive/postgres
```

### Step 5: Add TLS Certificates (Production)

For production, place your certificates in the `certs/` directory:

```bash
cp /path/to/your/certificate.crt certs/server.crt
cp /path/to/your/private.key certs/server.key
chmod 600 certs/server.key
```

---

## Configuration

### Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_SECRET` | (generated) | GitHub webhook secret for signature verification |
| `POSTGRES_USER` | github_archive | PostgreSQL username |
| `POSTGRES_PASSWORD` | (generated) | PostgreSQL password |
| `POSTGRES_DB` | github_archive | Database name |
| `ALLOWED_ORGS` | | Comma-separated list of allowed GitHub orgs |
| `WEBHOOK_PORT` | 8443 | External HTTPS port |
| `IP_ALLOWLIST_MODE` | github | `github`, `custom`, or `disabled` |
| `CUSTOM_ALLOWED_IPS` | | Custom IP allowlist (if mode=custom) |
| `TLS_MODE` | selfsigned | `selfsigned`, `provided`, or `acme` |
| `DOMAIN` | localhost | Server domain name |
| `READ_API_ENABLED` | true | Enable read-only query API |
| `READ_API_AUTH_REQUIRED` | false | Require API key for reads |
| `READ_API_KEY` | (generated) | API key for read access |

### IP Allowlisting

The system supports three IP allowlist modes:

1. **github** (recommended): Only accepts webhooks from GitHub's published IP ranges
2. **custom**: Uses your custom IP list from `CUSTOM_ALLOWED_IPS`
3. **disabled**: Accepts from any IP (NOT recommended for production)

GitHub IPs are automatically updated every 6 hours by the `ip-updater` service.

---

## Deployment Options

### Option A: Single Server (Docker Compose)

```bash
cd /opt/github-archive

# Start all services
docker compose up -d

# View logs
docker compose logs -f

# Check status
docker compose ps
```

### Option B: High Availability

```bash
cd /opt/github-archive

# Start with HA overlay
docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d
```

This deploys:
- 2 archive service instances (load balanced)
- PostgreSQL primary + replica
- Shared JSON storage volume

### Option C: Podman (Rootless)

```bash
# Install podman-compose if needed
pip install podman-compose

# Start services
podman-compose up -d
```

### Option D: External PostgreSQL

If you have an existing PostgreSQL server:

1. Run the `db/init.sql` script on your database
2. Update `.env`:
   ```bash
   DATABASE_URL=postgresql+asyncpg://user:pass@your-db-host:5432/github_archive
   ```
3. Remove the postgres service from docker-compose.yml or use an override

---

## GitHub Webhook Setup

### Step 1: Get Your Webhook Secret

```bash
grep WEBHOOK_SECRET .env
# Save this value - you'll need it in GitHub
```

### Step 2: Configure Organization Webhook

1. Go to your GitHub organization: `https://github.com/organizations/YOUR-ORG/settings/hooks`
2. Click **Add webhook**
3. Configure:

| Setting | Value |
|---------|-------|
| Payload URL | `https://your-server:8443/webhook` |
| Content type | `application/json` |
| Secret | (paste WEBHOOK_SECRET from .env) |
| SSL verification | Enable (if using valid certs) |

4. Select events to trigger:
   - ✅ Issues
   - ✅ Issue comments
   - ✅ Pull requests
   - ✅ Pull request reviews
   - ✅ Pull request review comments
   - ✅ Pull request review threads
   - ✅ Discussions
   - ✅ Discussion comments
   - ✅ Projects (and items)
   - ✅ Releases
   - ✅ Milestones
   - ✅ Wiki
   - ✅ Teams
   - ✅ Memberships
   - ✅ Repositories (metadata only)

5. Click **Add webhook**

### Step 3: Verify Webhook Delivery

1. After creating the webhook, GitHub sends a `ping` event
2. Check webhook delivery status in GitHub
3. View archive logs: `docker compose logs -f archive`

---

## Verification

### Health Check

```bash
# Check service health
curl -k https://localhost:8443/health

# Expected response:
{
  "status": "healthy",
  "database": "connected",
  "total_events": 0,
  "timestamp": "2024-01-15T12:00:00Z"
}
```

### Test Webhook (Manual)

Create a test issue in one of your repositories, then verify it was captured:

```bash
# Query recent events
curl -k https://localhost:8443/api/v1/events?limit=5

# Check database directly
docker compose exec postgres psql -U github_archive -c \
  "SELECT event_type, action, organization, received_at FROM webhook_events ORDER BY received_at DESC LIMIT 5;"
```

### Integrity Verification

```bash
# Run integrity check
python3 scripts/verify-integrity.py \
  --database-url "postgresql://github_archive:PASSWORD@localhost:5432/github_archive"
```

---

## Operations

### Daily Operations

```bash
# View service status
docker compose ps

# View recent logs
docker compose logs --tail=100 archive

# Check event counts
docker compose exec postgres psql -U github_archive -c \
  "SELECT date(received_at), event_type, count(*) FROM webhook_events GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC LIMIT 20;"
```

### Backup

```bash
# Run backup script
./scripts/backup.sh

# Backups are stored in /var/lib/github-archive/backups/
```

### Export for Compliance

```bash
# Full export
python3 scripts/nara-export.py \
  --database-url "postgresql://..." \
  --output-dir /path/to/exports \
  --format both

# Date range export
python3 scripts/nara-export.py \
  --since "2024-01-01" \
  --until "2024-03-31" \
  --organization "NOAA-GFDL" \
  --output-dir /path/to/quarterly-export
```

### Backfill Existing Data

Before webhooks capture new activity, import existing content:

```bash
# Set GitHub token
export GITHUB_TOKEN="ghp_your_token_here"

# Backfill entire organization
python3 scripts/backfill.py \
  --database-url "postgresql://..." \
  --org NOAA-GFDL \
  --org NOAA-RDHPCS

# Backfill specific repository
python3 scripts/backfill.py \
  --database-url "postgresql://..." \
  --repo NOAA-GFDL/FMS
```

### Update GitHub IPs

IPs are updated automatically, but you can trigger manually:

```bash
./scripts/update-github-ips.sh
```

---

## Troubleshooting

### Webhook Delivery Failures

**Symptom**: GitHub shows webhook delivery failures

1. Check if service is running:
   ```bash
   docker compose ps
   curl -k https://localhost:8443/health
   ```

2. Check firewall allows GitHub IPs:
   ```bash
   # View current allowlist
   cat traefik/dynamic/github-ips.yml
   ```

3. Check signature verification:
   ```bash
   docker compose logs archive | grep -i signature
   ```

4. Verify webhook secret matches:
   ```bash
   grep WEBHOOK_SECRET .env
   # Must match secret in GitHub webhook settings
   ```

### Database Connection Errors

```bash
# Check PostgreSQL is running
docker compose ps postgres
docker compose logs postgres

# Test connection
docker compose exec postgres pg_isready -U github_archive
```

### High Memory Usage

```bash
# Check container resource usage
docker stats

# Increase PostgreSQL shared_buffers if needed
# Edit docker-compose.yml, add under postgres command:
# - -c
# - shared_buffers=512MB
```

### Missing Events

1. Verify webhook is configured for all event types
2. Check if organization is in `ALLOWED_ORGS`
3. Review archive logs for rejected events

---

## Security Considerations

### Network Security

- Deploy behind a firewall
- Use IP allowlisting (enabled by default)
- Use valid TLS certificates in production
- Consider VPN for database access

### Database Security

- Database triggers prevent UPDATE/DELETE
- Use strong passwords (auto-generated by setup)
- Separate read-only user for queries
- Regular backups with integrity verification

### Access Control

- Webhook secret prevents unauthorized submissions
- Optional API key for read access
- All access is logged to `access_log` table

### Compliance

- Immutable records meet NARA requirements
- SHA-256 checksums verify integrity
- Complete version history preserved
- Export tools generate compliance-ready packages

---

## Support

For issues specific to this deployment:
1. Check the troubleshooting section above
2. Review logs: `docker compose logs`
3. Verify configuration: `docker compose config`

For GitHub webhook issues:
- Check delivery status in GitHub webhook settings
- Review GitHub's [webhook documentation](https://docs.github.com/en/webhooks)
