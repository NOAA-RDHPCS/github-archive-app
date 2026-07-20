# GitHub Archive System - Operations Guide

## Table of Contents

1. [Routine Operations](#routine-operations)
2. [Monitoring](#monitoring)
3. [Backup and Recovery](#backup-and-recovery)
4. [Maintenance Tasks](#maintenance-tasks)
5. [Scaling](#scaling)
6. [Incident Response](#incident-response)

---

## Routine Operations

### Daily Checklist

```bash
# 1. Verify services are running
docker compose ps

# 2. Check health endpoint
curl -k https://localhost:8443/health

# 3. Review recent events (should show activity from last 24h)
curl -k "https://localhost:8443/api/v1/events?limit=10"

# 4. Check for errors in logs
docker compose logs --since 24h archive | grep -i error

# 5. Verify disk space
df -h /var/lib/github-archive/
```

### Weekly Checklist

```bash
# 1. Run integrity verification
python3 scripts/verify-integrity.py --database-url "$DATABASE_URL"

# 2. Review storage growth
du -sh /var/lib/github-archive/*

# 3. Check database size
docker compose exec postgres psql -U github_archive -c \
  "SELECT pg_size_pretty(pg_database_size('github_archive'));"

# 4. Review access logs
docker compose exec postgres psql -U github_archive -c \
  "SELECT date(timestamp), action, count(*) FROM access_log 
   WHERE timestamp > now() - interval '7 days'
   GROUP BY 1, 2 ORDER BY 1 DESC;"

# 5. Verify GitHub IP list is current
cat traefik/dynamic/github-ips.yml | head -5
```

### Monthly Checklist

```bash
# 1. Full database backup with verification
./scripts/backup.sh
sha256sum -c /var/lib/github-archive/backups/latest.sha256

# 2. Generate compliance report
python3 scripts/nara-export.py \
  --since "$(date -d '1 month ago' +%Y-%m-%d)" \
  --output-dir "/var/lib/github-archive/exports/$(date +%Y-%m)"

# 3. Review and rotate logs
docker compose logs --since 720h > /var/log/github-archive/monthly-$(date +%Y%m).log
gzip /var/log/github-archive/monthly-$(date +%Y%m).log

# 4. Update container images (if new versions available)
docker compose pull
docker compose up -d

# 5. Test restore procedure (on non-production)
```

---

## Monitoring

### Prometheus Metrics

The archive service exposes metrics at `https://localhost:8443/metrics`:

```
# HELP github_archive_events_total Total webhook events received
# TYPE github_archive_events_total counter
github_archive_events_total 12345

# HELP github_archive_content_versions_total Total content versions stored
# TYPE github_archive_content_versions_total counter
github_archive_content_versions_total 45678

# HELP github_archive_events_by_type Events by type
# TYPE github_archive_events_by_type counter
github_archive_events_by_type{type="issues"} 1234
github_archive_events_by_type{type="issue_comment"} 5678
github_archive_events_by_type{type="pull_request"} 2345
```

### Alerting Rules (Prometheus)

```yaml
groups:
  - name: github-archive
    rules:
      - alert: ArchiveServiceDown
        expr: up{job="github-archive"} == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "GitHub Archive service is down"
          
      - alert: NoEventsReceived
        expr: increase(github_archive_events_total[1h]) == 0
        for: 2h
        labels:
          severity: warning
        annotations:
          summary: "No webhook events received in 2 hours"
          
      - alert: HighErrorRate
        expr: rate(github_archive_errors_total[5m]) > 0.1
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "High error rate in archive service"
          
      - alert: DiskSpaceLow
        expr: node_filesystem_avail_bytes{mountpoint="/var/lib/github-archive"} < 10737418240
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Less than 10GB disk space remaining"
```

### Log Monitoring

Key log patterns to monitor:

```bash
# Successful event archival
docker compose logs archive | grep "Archived"

# Signature verification failures (potential attacks)
docker compose logs archive | grep -i "invalid signature"

# IP rejection (misconfigured allowlist or attack)
docker compose logs archive | grep -i "ip not allowed"

# Database errors
docker compose logs archive | grep -i "database\|postgres\|connection"

# Rate limiting from GitHub
docker compose logs archive | grep -i "rate limit"
```

### Health Check Script

Create `/opt/github-archive/scripts/health-check.sh`:

```bash
#!/bin/bash
# Health check script for monitoring systems

set -e

ENDPOINT="${1:-https://localhost:8443/health}"

response=$(curl -sk -w "\n%{http_code}" "$ENDPOINT")
http_code=$(echo "$response" | tail -1)
body=$(echo "$response" | head -n -1)

if [ "$http_code" != "200" ]; then
    echo "CRITICAL: Health check returned HTTP $http_code"
    exit 2
fi

status=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")

if [ "$status" != "healthy" ]; then
    echo "CRITICAL: Service status is $status"
    exit 2
fi

db_status=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['database'])")

if [ "$db_status" != "connected" ]; then
    echo "WARNING: Database status is $db_status"
    exit 1
fi

echo "OK: Service healthy, database connected"
exit 0
```

---

## Backup and Recovery

### Automated Backup Setup

Create a cron job for daily backups:

```bash
# Add to crontab
crontab -e

# Daily backup at 2 AM
0 2 * * * /opt/github-archive/scripts/backup.sh >> /var/log/github-archive/backup.log 2>&1

# Weekly integrity check on Sunday at 3 AM
0 3 * * 0 /opt/github-archive/scripts/verify-integrity.py >> /var/log/github-archive/integrity.log 2>&1
```

### Manual Backup

```bash
# Full database backup
./scripts/backup.sh

# Backup JSON files
tar -czf /backup/github-archive-json-$(date +%Y%m%d).tar.gz /var/lib/github-archive/json/

# Backup configuration
tar -czf /backup/github-archive-config-$(date +%Y%m%d).tar.gz \
  /opt/github-archive/.env \
  /opt/github-archive/certs/ \
  /opt/github-archive/traefik/
```

### Recovery Procedures

#### Scenario 1: Service Failure (No Data Loss)

```bash
# Restart services
docker compose down
docker compose up -d

# Verify health
curl -k https://localhost:8443/health
```

#### Scenario 2: Database Corruption

```bash
# Stop services
docker compose down

# Remove corrupted data
sudo rm -rf /var/lib/github-archive/postgres/*

# Restore from backup
gunzip -c /backup/github_archive_YYYYMMDD_HHMMSS.sql.gz | \
  docker compose exec -T postgres psql -U github_archive

# Restart services
docker compose up -d

# Verify data
docker compose exec postgres psql -U github_archive -c \
  "SELECT COUNT(*) FROM webhook_events;"
```

#### Scenario 3: Complete Server Failure

1. Provision new server with same specs
2. Install Docker and docker-compose
3. Copy backup files to new server
4. Restore configuration:
   ```bash
   tar -xzf github-archive-config-*.tar.gz -C /opt/
   ```
5. Restore database:
   ```bash
   docker compose up -d postgres
   gunzip -c backup.sql.gz | docker compose exec -T postgres psql -U github_archive
   ```
6. Restore JSON files:
   ```bash
   tar -xzf github-archive-json-*.tar.gz -C /
   ```
7. Start all services:
   ```bash
   docker compose up -d
   ```
8. Update DNS/firewall to point to new server
9. Verify webhook deliveries resume

---

## Maintenance Tasks

### Database Maintenance

```bash
# Analyze tables for query optimization (safe, no locks)
docker compose exec postgres psql -U github_archive -c "ANALYZE;"

# Check table sizes
docker compose exec postgres psql -U github_archive -c "
  SELECT 
    relname as table,
    pg_size_pretty(pg_total_relation_size(relid)) as total_size,
    pg_size_pretty(pg_relation_size(relid)) as table_size,
    pg_size_pretty(pg_indexes_size(relid)) as index_size
  FROM pg_catalog.pg_statio_user_tables
  ORDER BY pg_total_relation_size(relid) DESC;
"

# Check for bloat (run during low activity)
docker compose exec postgres psql -U github_archive -c "
  SELECT schemaname, tablename, 
         pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size
  FROM pg_tables 
  WHERE schemaname = 'public';
"
```

### Certificate Renewal

For Let's Encrypt (ACME mode):
- Certificates auto-renew via Traefik

For provided certificates:
```bash
# Replace certificates
cp /path/to/new/certificate.crt certs/server.crt
cp /path/to/new/private.key certs/server.key
chmod 600 certs/server.key

# Reload Traefik
docker compose restart traefik
```

### Container Updates

```bash
# Pull latest images
docker compose pull

# Recreate containers with new images
docker compose up -d

# Remove old images
docker image prune -f
```

### Log Rotation

Docker handles log rotation, but configure limits:

```bash
# Add to docker-compose.yml under each service
logging:
  driver: "json-file"
  options:
    max-size: "100m"
    max-file: "5"
```

---

## Scaling

### Vertical Scaling

Increase resources for existing containers:

```yaml
# docker-compose.override.yml
services:
  archive:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 8G
        reservations:
          cpus: '2'
          memory: 4G
  
  postgres:
    deploy:
      resources:
        limits:
          memory: 16G
    command:
      - postgres
      - -c
      - shared_buffers=4GB
      - -c
      - effective_cache_size=12GB
      - -c
      - work_mem=256MB
```

### Horizontal Scaling (Archive Service)

Add more archive instances:

```bash
# Scale archive service
docker compose up -d --scale archive=3
```

Traefik automatically load balances across instances.

### Database Scaling

For read-heavy workloads, add read replicas:

```bash
# Use HA configuration
docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d
```

Configure application to use replica for read API:
```bash
# In .env
READ_DATABASE_URL=postgresql+asyncpg://github_archive:pass@postgres-replica:5432/github_archive
```

---

## Incident Response

### Webhook Delivery Failures

**Symptoms**: GitHub shows failed deliveries, events missing from archive

**Response**:
1. Check service health: `curl -k https://localhost:8443/health`
2. Check logs for errors: `docker compose logs --tail=100 archive`
3. Verify IP allowlist includes GitHub: `cat traefik/dynamic/github-ips.yml`
4. Check network connectivity to GitHub: `curl -I https://api.github.com`
5. If signature errors, verify WEBHOOK_SECRET matches GitHub

**Recovery**:
- GitHub retries failed deliveries for up to 3 days
- After fixing the issue, check GitHub webhook settings for "Redeliver" option

### Database Issues

**Symptoms**: Health check shows database disconnected, events not being stored

**Response**:
1. Check PostgreSQL status: `docker compose ps postgres`
2. Check PostgreSQL logs: `docker compose logs postgres`
3. Test connection: `docker compose exec postgres pg_isready`
4. Check disk space: `df -h /var/lib/github-archive/postgres/`

**Recovery**:
```bash
# Restart PostgreSQL
docker compose restart postgres

# If corruption suspected, restore from backup
docker compose down postgres
sudo rm -rf /var/lib/github-archive/postgres/*
docker compose up -d postgres
gunzip -c backup.sql.gz | docker compose exec -T postgres psql -U github_archive
```

### Security Incidents

**Symptoms**: Unexpected access in logs, integrity check failures

**Response**:
1. Check access logs:
   ```sql
   SELECT * FROM access_log WHERE timestamp > now() - interval '24 hours' ORDER BY timestamp DESC;
   ```
2. Run integrity verification:
   ```bash
   python3 scripts/verify-integrity.py --output incident-report.json
   ```
3. Review webhook events for suspicious sources:
   ```sql
   SELECT source_ip, count(*) FROM webhook_events 
   WHERE received_at > now() - interval '24 hours'
   GROUP BY source_ip;
   ```

**Recovery**:
- If integrity violations found, preserve evidence
- Rotate WEBHOOK_SECRET and API keys
- Review and update IP allowlist
- Contact security team per incident response policy

### Capacity Issues

**Symptoms**: Slow responses, disk space warnings, memory pressure

**Response**:
1. Check resource usage: `docker stats`
2. Check disk space: `df -h /var/lib/github-archive/`
3. Check database size:
   ```sql
   SELECT pg_size_pretty(pg_database_size('github_archive'));
   ```

**Recovery**:
- Short-term: Scale vertically (add resources)
- Medium-term: Scale horizontally (add instances)
- Long-term: Archive old data, optimize queries, increase storage
