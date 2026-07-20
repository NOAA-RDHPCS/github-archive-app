#!/bin/bash
# Setup script for GitHub Archive System
# Run this before first deployment

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "=========================================="
echo "GitHub Archive System - Setup"
echo "=========================================="

# Check for required tools
check_requirements() {
    echo "Checking requirements..."
    
    local missing=()
    
    if ! command -v docker &> /dev/null; then
        missing+=("docker")
    fi
    
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        missing+=("docker-compose")
    fi
    
    if ! command -v openssl &> /dev/null; then
        missing+=("openssl")
    fi
    
    if [ ${#missing[@]} -ne 0 ]; then
        echo "ERROR: Missing required tools: ${missing[*]}"
        echo "Please install them and run this script again."
        exit 1
    fi
    
    echo "✓ All requirements met"
}

# Create directory structure
create_directories() {
    echo "Creating directory structure..."
    
    local dirs=(
        "/var/lib/github-archive/json"
        "/var/lib/github-archive/postgres"
        "/var/lib/github-archive/postgres-primary"
        "/var/lib/github-archive/postgres-replica"
    )
    
    for dir in "${dirs[@]}"; do
        if [ ! -d "$dir" ]; then
            sudo mkdir -p "$dir"
            sudo chown -R 1000:1000 "$dir"
            echo "  Created: $dir"
        else
            echo "  Exists: $dir"
        fi
    done
    
    echo "✓ Directories created"
}

# Generate self-signed certificate
generate_certs() {
    echo "Generating TLS certificates..."
    
    local cert_dir="$SCRIPT_DIR/../certs"
    mkdir -p "$cert_dir"
    
    if [ -f "$cert_dir/server.crt" ] && [ -f "$cert_dir/server.key" ]; then
        echo "  Certificates already exist. Skipping generation."
        echo "  Delete certs/ directory to regenerate."
        return
    fi
    
    # Generate private key and self-signed certificate
    openssl req -x509 -nodes -days 365 -newkey rsa:4096 \
        -keyout "$cert_dir/server.key" \
        -out "$cert_dir/server.crt" \
        -subj "/C=US/ST=State/L=City/O=Organization/CN=github-archive.local" \
        -addext "subjectAltName=DNS:github-archive.local,DNS:localhost,IP:127.0.0.1"
    
    chmod 600 "$cert_dir/server.key"
    chmod 644 "$cert_dir/server.crt"
    
    echo "✓ Self-signed certificates generated"
    echo "  NOTE: For production, replace with proper certificates"
}

# Create .env file if not exists
create_env_file() {
    echo "Checking environment configuration..."
    
    if [ -f "$SCRIPT_DIR/../.env" ]; then
        echo "  .env file already exists"
        return
    fi
    
    echo "Creating .env file from template..."
    cp "$SCRIPT_DIR/../.env.example" "$SCRIPT_DIR/../.env"
    
    # Generate random secrets
    local webhook_secret=$(openssl rand -hex 32)
    local postgres_password=$(openssl rand -hex 24)
    local api_key=$(openssl rand -hex 32)
    local replication_password=$(openssl rand -hex 24)
    
    # Update .env with generated secrets
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS sed
        sed -i '' "s/your-webhook-secret-here/$webhook_secret/" "$SCRIPT_DIR/../.env"
        sed -i '' "s/change-this-to-a-strong-password/$postgres_password/" "$SCRIPT_DIR/../.env"
        sed -i '' "s/your-read-api-key-here/$api_key/" "$SCRIPT_DIR/../.env"
        sed -i '' "s/change-this-replication-password/$replication_password/" "$SCRIPT_DIR/../.env"
    else
        # Linux sed
        sed -i "s/your-webhook-secret-here/$webhook_secret/" "$SCRIPT_DIR/../.env"
        sed -i "s/change-this-to-a-strong-password/$postgres_password/" "$SCRIPT_DIR/../.env"
        sed -i "s/your-read-api-key-here/$api_key/" "$SCRIPT_DIR/../.env"
        sed -i "s/change-this-replication-password/$replication_password/" "$SCRIPT_DIR/../.env"
    fi
    
    echo "✓ .env file created with generated secrets"
    echo ""
    echo "IMPORTANT: Edit .env to configure:"
    echo "  - ALLOWED_ORGS: Your GitHub organization names"
    echo "  - DOMAIN: Your server's domain name"
    echo "  - TLS_MODE: Certificate mode (selfsigned/provided/acme)"
    echo ""
    echo "Save the WEBHOOK_SECRET - you'll need it when configuring the GitHub webhook:"
    echo "  WEBHOOK_SECRET=$webhook_secret"
}

# Fetch initial GitHub IPs
fetch_github_ips() {
    echo "Fetching GitHub webhook IP ranges..."
    
    local ip_file="$SCRIPT_DIR/../traefik/dynamic/github-ips.yml"
    
    # Fetch from GitHub API
    local ips=$(curl -s https://api.github.com/meta | python3 -c "
import sys, json
data = json.load(sys.stdin)
hooks = data.get('hooks', [])
for ip in hooks:
    print(f'          - \"{ip}\"')
" 2>/dev/null)
    
    if [ -z "$ips" ]; then
        echo "  WARNING: Could not fetch GitHub IPs. Using defaults."
        return
    fi
    
    # Update the middlewares file
    cat > "$ip_file" << EOF
# Auto-generated GitHub IP ranges
# Last updated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Source: https://api.github.com/meta

http:
  middlewares:
    github-ip-allowlist:
      ipAllowList:
        sourceRange:
$ips
EOF
    
    echo "✓ GitHub IPs updated"
}

# Build Docker images
build_images() {
    echo "Building Docker images..."
    
    cd "$SCRIPT_DIR/.."
    
    if docker compose version &> /dev/null; then
        docker compose build
    else
        docker-compose build
    fi
    
    echo "✓ Images built"
}

# Print next steps
print_next_steps() {
    echo ""
    echo "=========================================="
    echo "Setup Complete!"
    echo "=========================================="
    echo ""
    echo "Next steps:"
    echo ""
    echo "1. Review and edit .env file:"
    echo "   nano .env"
    echo ""
    echo "2. Start the services:"
    echo "   docker compose up -d"
    echo ""
    echo "   Or for high-availability:"
    echo "   docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d"
    echo ""
    echo "3. Configure GitHub organization webhook:"
    echo "   - Go to: https://github.com/organizations/YOUR-ORG/settings/hooks"
    echo "   - Payload URL: https://YOUR-SERVER:8443/webhook"
    echo "   - Content type: application/json"
    echo "   - Secret: (from .env WEBHOOK_SECRET)"
    echo "   - Select events: Issues, Issue comments, Pull requests,"
    echo "                    Pull request reviews, Pull request review comments,"
    echo "                    Pull request review threads, Discussions,"
    echo "                    Discussion comments, Projects v2, Releases,"
    echo "                    Milestones, Wiki, Teams, Memberships"
    echo ""
    echo "4. Verify the installation:"
    echo "   curl -k https://localhost:8443/health"
    echo ""
    echo "5. View logs:"
    echo "   docker compose logs -f"
    echo ""
}

# Main
main() {
    check_requirements
    create_directories
    generate_certs
    create_env_file
    fetch_github_ips
    build_images
    print_next_steps
}

main "$@"
