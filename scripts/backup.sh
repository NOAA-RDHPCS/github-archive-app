#!/bin/bash
# Database backup script for GitHub Archive System
# Creates timestamped PostgreSQL dumps

set -e

# Configuration
BACKUP_DIR="${BACKUP_DIR:-/var/lib/github-archive/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-github-archive-postgres}"
POSTGRES_USER="${POSTGRES_USER:-github_archive}"
POSTGRES_DB="${POSTGRES_DB:-github_archive}"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Generate timestamp
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/github_archive_${TIMESTAMP}.sql.gz"

echo "=========================================="
echo "GitHub Archive Database Backup"
echo "=========================================="
echo "Timestamp: $TIMESTAMP"
echo "Output: $BACKUP_FILE"
echo ""

# Check if running in Docker context
if command -v docker &> /dev/null && docker ps -q -f name="$POSTGRES_CONTAINER" &> /dev/null; then
    echo "Using Docker container: $POSTGRES_CONTAINER"
    
    # Dump via Docker
    docker exec "$POSTGRES_CONTAINER" pg_dump \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        --format=plain \
        --no-owner \
        --no-acl \
        | gzip > "$BACKUP_FILE"
else
    echo "Using direct PostgreSQL connection"
    
    # Dump directly (assumes pg_dump is available and .pgpass is configured)
    pg_dump \
        -h "${POSTGRES_HOST:-localhost}" \
        -p "${POSTGRES_PORT:-5432}" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        --format=plain \
        --no-owner \
        --no-acl \
        | gzip > "$BACKUP_FILE"
fi

# Verify backup
BACKUP_SIZE=$(stat -f%z "$BACKUP_FILE" 2>/dev/null || stat -c%s "$BACKUP_FILE" 2>/dev/null)
if [ "$BACKUP_SIZE" -lt 1000 ]; then
    echo "ERROR: Backup file is too small ($BACKUP_SIZE bytes). Backup may have failed."
    rm -f "$BACKUP_FILE"
    exit 1
fi

echo "Backup created: $BACKUP_FILE ($(numfmt --to=iec-i --suffix=B $BACKUP_SIZE 2>/dev/null || echo "$BACKUP_SIZE bytes"))"

# Create checksum
sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"
echo "Checksum: $BACKUP_FILE.sha256"

# Cleanup old backups
if [ "$RETENTION_DAYS" -gt 0 ]; then
    echo ""
    echo "Cleaning up backups older than $RETENTION_DAYS days..."
    find "$BACKUP_DIR" -name "github_archive_*.sql.gz" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
    find "$BACKUP_DIR" -name "github_archive_*.sql.gz.sha256" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
fi

# List recent backups
echo ""
echo "Recent backups:"
ls -lh "$BACKUP_DIR"/github_archive_*.sql.gz 2>/dev/null | tail -5

echo ""
echo "Backup complete!"
