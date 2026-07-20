#!/bin/bash
# Update GitHub webhook IP addresses in Traefik config
# Run periodically via cron or the ip-updater container

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_FILE="${OUTPUT_FILE:-$SCRIPT_DIR/../traefik/dynamic/github-ips.yml}"

echo "Fetching GitHub webhook IP ranges..."

# Fetch from GitHub API
IPS=$(curl -s https://api.github.com/meta | python3 -c "
import sys, json
data = json.load(sys.stdin)
hooks = data.get('hooks', [])
for ip in hooks:
    print(f'          - \"{ip}\"')
")

if [ -z "$IPS" ]; then
    echo "ERROR: Could not fetch GitHub IPs"
    exit 1
fi

# Count IPs
IP_COUNT=$(echo "$IPS" | wc -l | tr -d ' ')
echo "Found $IP_COUNT IP ranges"

# Write config file
cat > "$OUTPUT_FILE" << EOF
# Auto-generated GitHub IP ranges
# Last updated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Source: https://api.github.com/meta
# IP count: $IP_COUNT

http:
  middlewares:
    github-ip-allowlist:
      ipAllowList:
        sourceRange:
$IPS
EOF

echo "Updated: $OUTPUT_FILE"

# Traefik should auto-reload since we're using file provider with watch: true
echo "Traefik will auto-reload the configuration"
