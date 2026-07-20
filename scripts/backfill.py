#!/usr/bin/env python3
"""
GitHub Backfill Tool for Archive System

Fetches existing issues, pull requests, discussions, and other content
from GitHub organizations that existed before the webhook was configured.

This tool captures the current state of all content items and imports them
as version 1 records in the archive system.

Requirements:
- GitHub Personal Access Token with appropriate scopes
- psycopg2-binary
- requests
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Generator

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, execute_values
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GitHubAPI:
    """GitHub API client with rate limiting and pagination"""
    
    def __init__(self, token: str):
        self.token = token
        self.base_url = "https://api.github.com"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        })
        self.rate_limit_remaining = 5000
        self.rate_limit_reset = 0
    
    def _check_rate_limit(self, response: requests.Response):
        """Update rate limit tracking from response headers"""
        self.rate_limit_remaining = int(response.headers.get("X-RateLimit-Remaining", 5000))
        self.rate_limit_reset = int(response.headers.get("X-RateLimit-Reset", 0))
        
        if self.rate_limit_remaining < 100:
            wait_time = self.rate_limit_reset - time.time() + 5
            if wait_time > 0:
                logger.warning(f"Rate limit low ({self.rate_limit_remaining}). Waiting {wait_time:.0f}s...")
                time.sleep(wait_time)
    
    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make API request with rate limiting"""
        url = f"{self.base_url}{endpoint}" if endpoint.startswith("/") else endpoint
        
        response = self.session.request(method, url, **kwargs)
        self._check_rate_limit(response)
        
        if response.status_code == 403 and "rate limit" in response.text.lower():
            wait_time = self.rate_limit_reset - time.time() + 5
            logger.warning(f"Rate limited. Waiting {wait_time:.0f}s...")
            time.sleep(max(wait_time, 60))
            response = self.session.request(method, url, **kwargs)
        
        return response
    
    def get(self, endpoint: str, **kwargs) -> requests.Response:
        return self._request("GET", endpoint, **kwargs)
    
    def paginate(self, endpoint: str, params: Dict = None) -> Generator[Dict, None, None]:
        """Paginate through API results"""
        params = params or {}
        params.setdefault("per_page", 100)
        
        while endpoint:
            response = self.get(endpoint, params=params)
            
            if response.status_code != 200:
                logger.error(f"API error {response.status_code}: {response.text[:200]}")
                break
            
            data = response.json()
            
            # Handle different response formats
            if isinstance(data, list):
                for item in data:
                    yield item
            elif isinstance(data, dict) and "items" in data:
                for item in data["items"]:
                    yield item
            else:
                yield data
                break
            
            # Get next page URL from Link header
            link_header = response.headers.get("Link", "")
            endpoint = None
            for link in link_header.split(","):
                if 'rel="next"' in link:
                    endpoint = link.split(";")[0].strip("<> ")
                    params = {}  # URL already includes params
                    break


class ArchiveBackfill:
    """Backfill existing GitHub content into archive database"""
    
    def __init__(self, github_token: str, database_url: str):
        self.github = GitHubAPI(github_token)
        self.database_url = database_url
        self.conn = None
        self.stats = {
            "repos_processed": 0,
            "issues_imported": 0,
            "issue_comments_imported": 0,
            "pull_requests_imported": 0,
            "pr_reviews_imported": 0,
            "pr_review_comments_imported": 0,
            "discussions_imported": 0,
            "discussion_comments_imported": 0,
            "releases_imported": 0,
            "milestones_imported": 0,
            "errors": 0
        }
    
    def connect(self):
        """Establish database connection"""
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            url = url.replace("postgresql+asyncpg://", "postgresql://")
        self.conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
    
    def compute_checksum(self, data: dict) -> str:
        """Compute SHA-256 checksum"""
        json_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(json_str.encode()).hexdigest()
    
    def compute_content_checksum(self, title: str, body: str, metadata: dict) -> str:
        """Compute checksum for content version"""
        content = {
            "title": title or "",
            "body": body or "",
            "metadata": metadata or {}
        }
        return self.compute_checksum(content)
    
    def parse_datetime(self, dt_str: Optional[str]) -> Optional[datetime]:
        """Parse ISO datetime string"""
        if not dt_str:
            return None
        try:
            if dt_str.endswith('Z'):
                dt_str = dt_str[:-1] + '+00:00'
            return datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            return None
    
    def record_exists(self, content_type: str, github_id: int) -> bool:
        """Check if a record already exists in the archive"""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM content_versions WHERE content_type = %s AND github_id = %s LIMIT 1",
                (content_type, github_id)
            )
            return cur.fetchone() is not None
    
    def insert_webhook_event(
        self,
        event_type: str,
        action: str,
        organization: str,
        repository: str,
        payload: dict,
        github_id: int = None
    ) -> str:
        """Insert a synthetic webhook event for backfilled content"""
        event_id = str(uuid.uuid4())
        delivery_id = f"backfill-{event_id}"
        checksum = self.compute_checksum(payload)
        
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO webhook_events 
                (id, delivery_id, event_type, action, organization, repository, 
                 sender, payload, signature, source_ip, checksum, github_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                event_id, delivery_id, event_type, action, organization, repository,
                "backfill", json.dumps(payload), "backfill", "127.0.0.1", checksum, github_id
            ))
        
        return event_id
    
    def insert_content_version(
        self,
        webhook_event_id: str,
        content_type: str,
        github_id: int,
        title: str,
        body: str,
        state: str,
        metadata: dict,
        actor_login: str,
        actor_id: int,
        github_node_id: str = None,
        github_created_at: str = None,
        github_updated_at: str = None
    ):
        """Insert a content version record"""
        checksum = self.compute_content_checksum(title, body, metadata)
        
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO content_versions
                (webhook_event_id, content_type, github_id, github_node_id,
                 version_number, title, body, state, metadata,
                 actor_login, actor_id, github_created_at, github_updated_at, checksum)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                webhook_event_id, content_type, github_id, github_node_id,
                1, title, body, state, json.dumps(metadata),
                actor_login, actor_id,
                self.parse_datetime(github_created_at),
                self.parse_datetime(github_updated_at),
                checksum
            ))
    
    def backfill_issues(self, org: str, repo: str):
        """Backfill all issues for a repository"""
        logger.info(f"  Backfilling issues for {org}/{repo}...")
        
        endpoint = f"/repos/{org}/{repo}/issues"
        params = {"state": "all", "sort": "created", "direction": "asc"}
        
        for issue in self.github.paginate(endpoint, params):
            # Skip pull requests (they come through the issues API too)
            if "pull_request" in issue:
                continue
            
            github_id = issue["id"]
            
            # Skip if already exists
            if self.record_exists("issue", github_id):
                continue
            
            try:
                metadata = {
                    "labels": [l["name"] for l in issue.get("labels", [])],
                    "assignees": [a["login"] for a in issue.get("assignees", [])],
                    "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
                    "url": issue["html_url"],
                    "number": issue["number"],
                    "locked": issue.get("locked", False),
                    "action": "backfill"
                }
                
                # Create synthetic event
                event_id = self.insert_webhook_event(
                    event_type="issues",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"issue": issue},
                    github_id=github_id
                )
                
                # Create content version
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="issue",
                    github_id=github_id,
                    title=issue.get("title"),
                    body=issue.get("body"),
                    state=issue.get("state"),
                    metadata=metadata,
                    actor_login=issue["user"]["login"],
                    actor_id=issue["user"]["id"],
                    github_node_id=issue.get("node_id"),
                    github_created_at=issue.get("created_at"),
                    github_updated_at=issue.get("updated_at")
                )
                
                self.stats["issues_imported"] += 1
                
                # Backfill comments for this issue
                self._backfill_issue_comments(org, repo, issue["number"])
                
            except Exception as e:
                logger.error(f"Error importing issue {github_id}: {e}")
                self.stats["errors"] += 1
        
        self.conn.commit()
    
    def _backfill_issue_comments(self, org: str, repo: str, issue_number: int):
        """Backfill comments for an issue"""
        endpoint = f"/repos/{org}/{repo}/issues/{issue_number}/comments"
        
        for comment in self.github.paginate(endpoint):
            github_id = comment["id"]
            
            if self.record_exists("issue_comment", github_id):
                continue
            
            try:
                metadata = {
                    "issue_number": issue_number,
                    "url": comment["html_url"],
                    "author_association": comment.get("author_association"),
                    "action": "backfill"
                }
                
                event_id = self.insert_webhook_event(
                    event_type="issue_comment",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"comment": comment},
                    github_id=github_id
                )
                
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="issue_comment",
                    github_id=github_id,
                    title=None,
                    body=comment.get("body"),
                    state=None,
                    metadata=metadata,
                    actor_login=comment["user"]["login"],
                    actor_id=comment["user"]["id"],
                    github_node_id=comment.get("node_id"),
                    github_created_at=comment.get("created_at"),
                    github_updated_at=comment.get("updated_at")
                )
                
                self.stats["issue_comments_imported"] += 1
                
            except Exception as e:
                logger.error(f"Error importing comment {github_id}: {e}")
                self.stats["errors"] += 1
    
    def backfill_pull_requests(self, org: str, repo: str):
        """Backfill all pull requests for a repository"""
        logger.info(f"  Backfilling pull requests for {org}/{repo}...")
        
        endpoint = f"/repos/{org}/{repo}/pulls"
        params = {"state": "all", "sort": "created", "direction": "asc"}
        
        for pr in self.github.paginate(endpoint, params):
            github_id = pr["id"]
            
            if self.record_exists("pull_request", github_id):
                continue
            
            try:
                metadata = {
                    "labels": [l["name"] for l in pr.get("labels", [])],
                    "assignees": [a["login"] for a in pr.get("assignees", [])],
                    "reviewers": [r["login"] for r in pr.get("requested_reviewers", [])],
                    "milestone": pr.get("milestone", {}).get("title") if pr.get("milestone") else None,
                    "url": pr["html_url"],
                    "number": pr["number"],
                    "draft": pr.get("draft", False),
                    "merged": pr.get("merged", False),
                    "merged_by": pr.get("merged_by", {}).get("login") if pr.get("merged_by") else None,
                    "base_ref": pr.get("base", {}).get("ref"),
                    "head_ref": pr.get("head", {}).get("ref"),
                    "action": "backfill"
                }
                
                event_id = self.insert_webhook_event(
                    event_type="pull_request",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"pull_request": pr},
                    github_id=github_id
                )
                
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="pull_request",
                    github_id=github_id,
                    title=pr.get("title"),
                    body=pr.get("body"),
                    state=pr.get("state"),
                    metadata=metadata,
                    actor_login=pr["user"]["login"],
                    actor_id=pr["user"]["id"],
                    github_node_id=pr.get("node_id"),
                    github_created_at=pr.get("created_at"),
                    github_updated_at=pr.get("updated_at")
                )
                
                self.stats["pull_requests_imported"] += 1
                
                # Backfill reviews and review comments
                self._backfill_pr_reviews(org, repo, pr["number"])
                self._backfill_pr_review_comments(org, repo, pr["number"])
                
            except Exception as e:
                logger.error(f"Error importing PR {github_id}: {e}")
                self.stats["errors"] += 1
        
        self.conn.commit()
    
    def _backfill_pr_reviews(self, org: str, repo: str, pr_number: int):
        """Backfill reviews for a pull request"""
        endpoint = f"/repos/{org}/{repo}/pulls/{pr_number}/reviews"
        
        for review in self.github.paginate(endpoint):
            github_id = review["id"]
            
            if self.record_exists("pull_request_review", github_id):
                continue
            
            try:
                metadata = {
                    "pull_request_number": pr_number,
                    "url": review.get("html_url"),
                    "commit_id": review.get("commit_id"),
                    "author_association": review.get("author_association"),
                    "action": "backfill"
                }
                
                event_id = self.insert_webhook_event(
                    event_type="pull_request_review",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"review": review},
                    github_id=github_id
                )
                
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="pull_request_review",
                    github_id=github_id,
                    title=None,
                    body=review.get("body"),
                    state=review.get("state"),
                    metadata=metadata,
                    actor_login=review["user"]["login"],
                    actor_id=review["user"]["id"],
                    github_node_id=review.get("node_id"),
                    github_created_at=review.get("submitted_at"),
                    github_updated_at=review.get("submitted_at")
                )
                
                self.stats["pr_reviews_imported"] += 1
                
            except Exception as e:
                logger.error(f"Error importing review {github_id}: {e}")
                self.stats["errors"] += 1
    
    def _backfill_pr_review_comments(self, org: str, repo: str, pr_number: int):
        """Backfill review comments for a pull request"""
        endpoint = f"/repos/{org}/{repo}/pulls/{pr_number}/comments"
        
        for comment in self.github.paginate(endpoint):
            github_id = comment["id"]
            
            if self.record_exists("pull_request_review_comment", github_id):
                continue
            
            try:
                metadata = {
                    "pull_request_number": pr_number,
                    "url": comment["html_url"],
                    "path": comment.get("path"),
                    "position": comment.get("position"),
                    "line": comment.get("line"),
                    "side": comment.get("side"),
                    "diff_hunk": comment.get("diff_hunk"),
                    "commit_id": comment.get("commit_id"),
                    "in_reply_to_id": comment.get("in_reply_to_id"),
                    "author_association": comment.get("author_association"),
                    "action": "backfill"
                }
                
                event_id = self.insert_webhook_event(
                    event_type="pull_request_review_comment",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"comment": comment},
                    github_id=github_id
                )
                
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="pull_request_review_comment",
                    github_id=github_id,
                    title=None,
                    body=comment.get("body"),
                    state=None,
                    metadata=metadata,
                    actor_login=comment["user"]["login"],
                    actor_id=comment["user"]["id"],
                    github_node_id=comment.get("node_id"),
                    github_created_at=comment.get("created_at"),
                    github_updated_at=comment.get("updated_at")
                )
                
                self.stats["pr_review_comments_imported"] += 1
                
            except Exception as e:
                logger.error(f"Error importing review comment {github_id}: {e}")
                self.stats["errors"] += 1
    
    def backfill_releases(self, org: str, repo: str):
        """Backfill all releases for a repository"""
        logger.info(f"  Backfilling releases for {org}/{repo}...")
        
        endpoint = f"/repos/{org}/{repo}/releases"
        
        for release in self.github.paginate(endpoint):
            github_id = release["id"]
            
            if self.record_exists("release", github_id):
                continue
            
            try:
                metadata = {
                    "url": release["html_url"],
                    "tag_name": release.get("tag_name"),
                    "target_commitish": release.get("target_commitish"),
                    "draft": release.get("draft", False),
                    "prerelease": release.get("prerelease", False),
                    "assets": [{"name": a["name"], "url": a["browser_download_url"]} 
                              for a in release.get("assets", [])],
                    "action": "backfill"
                }
                
                event_id = self.insert_webhook_event(
                    event_type="release",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"release": release},
                    github_id=github_id
                )
                
                author = release.get("author", {})
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="release",
                    github_id=github_id,
                    title=release.get("name"),
                    body=release.get("body"),
                    state="draft" if release.get("draft") else "published",
                    metadata=metadata,
                    actor_login=author.get("login", "unknown"),
                    actor_id=author.get("id", 0),
                    github_node_id=release.get("node_id"),
                    github_created_at=release.get("created_at"),
                    github_updated_at=release.get("published_at")
                )
                
                self.stats["releases_imported"] += 1
                
            except Exception as e:
                logger.error(f"Error importing release {github_id}: {e}")
                self.stats["errors"] += 1
        
        self.conn.commit()
    
    def backfill_milestones(self, org: str, repo: str):
        """Backfill all milestones for a repository"""
        logger.info(f"  Backfilling milestones for {org}/{repo}...")
        
        endpoint = f"/repos/{org}/{repo}/milestones"
        params = {"state": "all"}
        
        for milestone in self.github.paginate(endpoint, params):
            github_id = milestone["id"]
            
            if self.record_exists("milestone", github_id):
                continue
            
            try:
                metadata = {
                    "url": milestone["html_url"],
                    "number": milestone["number"],
                    "due_on": milestone.get("due_on"),
                    "open_issues": milestone.get("open_issues"),
                    "closed_issues": milestone.get("closed_issues"),
                    "action": "backfill"
                }
                
                event_id = self.insert_webhook_event(
                    event_type="milestone",
                    action="backfill",
                    organization=org,
                    repository=f"{org}/{repo}",
                    payload={"milestone": milestone},
                    github_id=github_id
                )
                
                creator = milestone.get("creator", {})
                self.insert_content_version(
                    webhook_event_id=event_id,
                    content_type="milestone",
                    github_id=github_id,
                    title=milestone.get("title"),
                    body=milestone.get("description"),
                    state=milestone.get("state"),
                    metadata=metadata,
                    actor_login=creator.get("login", "unknown"),
                    actor_id=creator.get("id", 0),
                    github_node_id=milestone.get("node_id"),
                    github_created_at=milestone.get("created_at"),
                    github_updated_at=milestone.get("updated_at")
                )
                
                self.stats["milestones_imported"] += 1
                
            except Exception as e:
                logger.error(f"Error importing milestone {github_id}: {e}")
                self.stats["errors"] += 1
        
        self.conn.commit()
    
    def backfill_repository(self, org: str, repo: str):
        """Backfill all content for a repository"""
        logger.info(f"Processing repository: {org}/{repo}")
        
        self.backfill_issues(org, repo)
        self.backfill_pull_requests(org, repo)
        self.backfill_releases(org, repo)
        self.backfill_milestones(org, repo)
        
        self.stats["repos_processed"] += 1
    
    def backfill_organization(self, org: str, skip_archived: bool = True):
        """Backfill all repositories in an organization"""
        logger.info(f"Fetching repositories for organization: {org}")
        
        endpoint = f"/orgs/{org}/repos"
        params = {"type": "all", "sort": "created", "direction": "asc"}
        
        repos = list(self.github.paginate(endpoint, params))
        logger.info(f"Found {len(repos)} repositories")
        
        for repo in repos:
            if skip_archived and repo.get("archived"):
                logger.info(f"  Skipping archived repo: {repo['name']}")
                continue
            
            self.backfill_repository(org, repo["name"])
    
    def print_stats(self):
        """Print import statistics"""
        print("\n" + "=" * 60)
        print("BACKFILL STATISTICS")
        print("=" * 60)
        print(f"Repositories processed:     {self.stats['repos_processed']:,}")
        print(f"Issues imported:            {self.stats['issues_imported']:,}")
        print(f"Issue comments imported:    {self.stats['issue_comments_imported']:,}")
        print(f"Pull requests imported:     {self.stats['pull_requests_imported']:,}")
        print(f"PR reviews imported:        {self.stats['pr_reviews_imported']:,}")
        print(f"PR review comments imported:{self.stats['pr_review_comments_imported']:,}")
        print(f"Discussions imported:       {self.stats['discussions_imported']:,}")
        print(f"Discussion comments:        {self.stats['discussion_comments_imported']:,}")
        print(f"Releases imported:          {self.stats['releases_imported']:,}")
        print(f"Milestones imported:        {self.stats['milestones_imported']:,}")
        print(f"Errors:                     {self.stats['errors']:,}")
        print("=" * 60)
        
        total = sum(v for k, v in self.stats.items() if k != "errors" and k != "repos_processed")
        print(f"TOTAL CONTENT ITEMS:        {total:,}")


def main():
    parser = argparse.ArgumentParser(
        description='Backfill existing GitHub content into archive system'
    )
    parser.add_argument(
        '--token',
        default=os.environ.get('GITHUB_TOKEN'),
        help='GitHub Personal Access Token (or set GITHUB_TOKEN env var)'
    )
    parser.add_argument(
        '--database-url',
        default=os.environ.get('DATABASE_URL'),
        help='PostgreSQL connection URL (or set DATABASE_URL env var)'
    )
    parser.add_argument(
        '--org',
        action='append',
        dest='orgs',
        help='Organization to backfill (can specify multiple)'
    )
    parser.add_argument(
        '--repo',
        action='append',
        dest='repos',
        help='Specific repository to backfill (format: org/repo, can specify multiple)'
    )
    parser.add_argument(
        '--include-archived',
        action='store_true',
        help='Include archived repositories'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be imported without actually importing'
    )
    
    args = parser.parse_args()
    
    if not args.token:
        print("ERROR: GitHub token required. Set GITHUB_TOKEN or use --token")
        sys.exit(1)
    
    if not args.orgs and not args.repos:
        print("ERROR: Specify at least one --org or --repo")
        sys.exit(1)
    
    print("=" * 60)
    print("GitHub Archive - Backfill Tool")
    print("=" * 60)
    
    backfill = ArchiveBackfill(args.token, args.database_url)
    
    try:
        backfill.connect()
        logger.info("Connected to database")
        
        # Process specific repositories
        if args.repos:
            for repo_path in args.repos:
                if "/" not in repo_path:
                    logger.error(f"Invalid repo format: {repo_path} (expected: org/repo)")
                    continue
                org, repo = repo_path.split("/", 1)
                backfill.backfill_repository(org, repo)
        
        # Process organizations
        if args.orgs:
            for org in args.orgs:
                backfill.backfill_organization(org, skip_archived=not args.include_archived)
        
        backfill.print_stats()
        
    except KeyboardInterrupt:
        logger.info("\nBackfill interrupted by user")
        backfill.print_stats()
    finally:
        backfill.close()


if __name__ == '__main__':
    main()
