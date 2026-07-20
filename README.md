# GitHub Organization Archive System

A secure, immutable archival system for capturing all non-git activity from GitHub organizations.
Designed for federal records compliance with append-only storage and cryptographic integrity verification.

## Features

- **Immutable Storage**: PostgreSQL with triggers preventing UPDATE/DELETE operations
- **Full Version History**: Every edit creates a new version linked to the original
- **Cryptographic Integrity**: SHA-256 checksums on all records
- **Webhook Signature Verification**: HMAC-SHA256 validation of all incoming webhooks
- **IP Allowlisting**: Configurable allowlist with automatic GitHub IP updates
- **Containerized Deployment**: Docker Compose for single-server or multi-node setups
- **High Availability Ready**: PostgreSQL replication support for redundancy

## Captured Events

| Event Type | Content Captured |
|------------|------------------|
| `issues` | Issue body, title, labels, assignees, state changes |
| `issue_comment` | Comments on issues and PRs |
| `pull_request` | PR title, body, reviewers, merge status |
| `pull_request_review` | Review body, approval/rejection state |
| `pull_request_review_comment` | Inline code review comments |
| `pull_request_review_thread` | Thread resolution status |
| `discussion` | Discussion body, title, category, answers |
| `discussion_comment` | Discussion replies |
| `projects_v2` | Project board metadata |
| `projects_v2_item` | Project item changes |
| `release` | Release notes |
| `milestone` | Milestone descriptions |
| `gollum` | Wiki page content |
| `team` | Team metadata |
| `membership` | Team membership changes |
| `organization` | Organization-level changes |
| `repository` | Repository metadata (non-git) |

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your settings:
# - WEBHOOK_SECRET: Generate with `openssl rand -hex 32`
# - POSTGRES_PASSWORD: Strong database password
# - ALLOWED_ORGS: Comma-separated list of GitHub org names
```

### 2. Start Services (Single Server)

```bash
docker-compose up -d
```

### 3. Configure GitHub Organization Webhook

1. Go to your GitHub Organization → Settings → Webhooks → Add webhook
2. Payload URL: `https://your-server:8443/webhook`
3. Content type: `application/json`
4. Secret: (use the WEBHOOK_SECRET from your .env)
5. Select events:
   - Issues
   - Issue comments
   - Pull requests
   - Pull request reviews
   - Pull request review comments
   - Pull request review threads
   - Discussions
   - Discussion comments
   - Projects v2
   - Projects v2 items
   - Releases
   - Milestones
   - Wiki (Gollum)
   - Teams
   - Memberships
   - Organization
   - Repositories

### 4. Verify Installation

```bash
# Check service health
curl -k https://localhost:8443/health

# View recent events (read-only API)
curl -k https://localhost:8443/api/v1/events?limit=10
```

## Architecture

```
                                    ┌─────────────────────────────┐
                                    │     GitHub Organization     │
                                    └─────────────┬───────────────┘
                                                  │ Webhooks
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Docker Network                                    │
│  ┌──────────────┐    ┌──────────────────┐    ┌────────────────────────────┐ │
│  │   Traefik    │───▶│  Archive Service │───▶│  PostgreSQL (append-only)  │ │
│  │  (reverse    │    │  (Python/FastAPI)│    │  + Triggers preventing     │ │
│  │   proxy)     │    │                  │    │    UPDATE/DELETE           │ │
│  │  - TLS       │    │  - Signature     │    └────────────────────────────┘ │
│  │  - IP allow  │    │    verification  │                                   │
│  └──────────────┘    │  - Checksum gen  │    ┌────────────────────────────┐ │
│                      │  - Content       │───▶│  Volume: /archive          │ │
│                      │    extraction    │    │  (JSON backup files)       │ │
│                      └──────────────────┘    └────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Security

### Immutability Guarantees

1. **Database Level**: PostgreSQL triggers raise exceptions on UPDATE/DELETE
2. **Application Level**: No modification endpoints exist in the API
3. **File Level**: JSON archives written with restrictive permissions
4. **Integrity**: SHA-256 checksums stored and verifiable

### Access Control

- Webhook endpoint requires valid HMAC-SHA256 signature
- IP allowlisting via Traefik middleware (GitHub webhook IPs)
- Read-only API for querying (optional authentication)
- Database uses separate read-only user for queries

## Directory Structure

```
github-archive/
├── docker-compose.yml          # Main orchestration
├── docker-compose.ha.yml       # High-availability overlay
├── .env.example                # Environment template
├── traefik/
│   └── traefik.yml            # Reverse proxy config
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                # FastAPI application
│   ├── models.py              # SQLAlchemy models
│   ├── schemas.py             # Pydantic schemas
│   ├── security.py            # Signature verification, IP checks
│   ├── database.py            # Database connection
│   └── events/                # Event handlers by type
│       ├── __init__.py
│       ├── issues.py
│       ├── pull_requests.py
│       ├── discussions.py
│       └── ...
├── db/
│   └── init.sql               # Schema + immutability triggers
├── scripts/
│   ├── setup.sh               # Initial deployment setup
│   ├── update-github-ips.sh   # Fetch latest GitHub IPs
│   ├── verify-integrity.py    # Checksum verification tool
│   ├── nara-export.py         # NARA-compliant export tool
│   ├── backfill.py            # Import existing content
│   └── backup.sh              # Database backup script
├── monitoring/                 # Prometheus/Grafana stack
│   ├── prometheus.yml
│   ├── alerts.yml
│   ├── alertmanager.yml
│   └── grafana/
└── docs/
    ├── DEPLOYMENT.md          # Full deployment guide
    └── OPERATIONS.md          # Runbook for operators
```

## Compliance

This system is designed to support federal records requirements:

- **Immutability**: Records cannot be altered after creation
- **Completeness**: All versions of content are preserved
- **Integrity**: Cryptographic verification of all records
- **Auditability**: Full access logging and verification tools
- **Retention**: No automatic deletion; retention managed externally

## License

Internal use - NOAA
