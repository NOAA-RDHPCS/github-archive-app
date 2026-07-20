"""
Security utilities for webhook verification and IP allowlisting
"""

import hashlib
import hmac
import ipaddress
import logging
from typing import List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify GitHub webhook signature (HMAC-SHA256).
    
    Args:
        payload: Raw request body bytes
        signature: X-Hub-Signature-256 header value
        secret: Webhook secret configured in GitHub
    
    Returns:
        True if signature is valid, False otherwise
    """
    if not secret:
        logger.warning("No webhook secret configured - signature verification skipped")
        return True
    
    if not signature:
        logger.warning("No signature provided in request")
        return False
    
    # GitHub sends signature as "sha256=<hex_digest>"
    if not signature.startswith("sha256="):
        logger.warning(f"Invalid signature format: {signature[:20]}...")
        return False
    
    expected_signature = signature[7:]  # Remove "sha256=" prefix
    
    # Compute HMAC-SHA256
    computed = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(computed, expected_signature)


async def check_ip_allowed(
    db: AsyncSession,
    client_ip: str,
    mode: str,
    custom_ips: List[str]
) -> bool:
    """
    Check if client IP is in the allowlist.
    
    Args:
        db: Database session
        client_ip: Client IP address string
        mode: "github", "custom", or "disabled"
        custom_ips: List of custom allowed IPs/CIDRs (for custom mode)
    
    Returns:
        True if IP is allowed, False otherwise
    """
    if mode == "disabled":
        return True
    
    try:
        client_addr = ipaddress.ip_address(client_ip)
    except ValueError:
        logger.error(f"Invalid client IP address: {client_ip}")
        return False
    
    if mode == "github":
        # Check against cached GitHub IPs
        result = await db.execute(
            text("SELECT ip_range FROM github_ips WHERE category = 'hooks'")
        )
        github_ranges = result.fetchall()
        
        for row in github_ranges:
            try:
                network = ipaddress.ip_network(row.ip_range, strict=False)
                if client_addr in network:
                    return True
            except ValueError:
                continue
        
        logger.debug(f"IP {client_ip} not in GitHub allowlist ({len(github_ranges)} ranges checked)")
        return False
    
    elif mode == "custom":
        # Check against custom IP list
        for ip_entry in custom_ips:
            ip_entry = ip_entry.strip()
            if not ip_entry:
                continue
            
            try:
                # Try as network (CIDR)
                network = ipaddress.ip_network(ip_entry, strict=False)
                if client_addr in network:
                    return True
            except ValueError:
                try:
                    # Try as single IP
                    if client_addr == ipaddress.ip_address(ip_entry):
                        return True
                except ValueError:
                    logger.warning(f"Invalid IP/CIDR in custom list: {ip_entry}")
                    continue
        
        return False
    
    return False


async def get_github_ips(db: AsyncSession) -> List[str]:
    """Get cached GitHub webhook IP ranges"""
    result = await db.execute(
        text("SELECT ip_range FROM github_ips WHERE category = 'hooks'")
    )
    return [str(row.ip_range) for row in result.fetchall()]
