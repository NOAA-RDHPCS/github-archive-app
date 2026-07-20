"""
Event processing - extract content versions from webhook payloads
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models import WebhookEvent, ContentVersion

logger = logging.getLogger(__name__)


def compute_content_checksum(title: str, body: str, metadata: dict) -> str:
    """Compute SHA-256 checksum for content"""
    content = {
        "title": title or "",
        "body": body or "",
        "metadata": metadata or {}
    }
    json_str = json.dumps(content, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode()).hexdigest()


def parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime string from GitHub"""
    if not dt_str:
        return None
    try:
        # Handle GitHub's ISO format with Z suffix
        if dt_str.endswith('Z'):
            dt_str = dt_str[:-1] + '+00:00'
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None


async def get_latest_version_number(
    db: AsyncSession,
    content_type: str,
    github_id: int
) -> int:
    """Get the latest version number for a content item"""
    result = await db.execute(
        text("""
            SELECT MAX(version_number) FROM content_versions 
            WHERE content_type = :content_type AND github_id = :github_id
        """),
        {"content_type": content_type, "github_id": github_id}
    )
    max_version = result.scalar()
    return max_version or 0


async def get_previous_version_id(
    db: AsyncSession,
    content_type: str,
    github_id: int,
    version_number: int
) -> Optional[str]:
    """Get the ID of the previous version"""
    if version_number <= 1:
        return None
    
    result = await db.execute(
        text("""
            SELECT id FROM content_versions 
            WHERE content_type = :content_type 
              AND github_id = :github_id 
              AND version_number = :version_number
        """),
        {"content_type": content_type, "github_id": github_id, "version_number": version_number - 1}
    )
    row = result.fetchone()
    return str(row.id) if row else None


async def create_content_version(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    content_type: str,
    github_id: int,
    title: Optional[str],
    body: Optional[str],
    state: Optional[str],
    metadata: dict,
    actor_login: str,
    actor_id: int,
    github_node_id: Optional[str] = None,
    github_created_at: Optional[str] = None,
    github_updated_at: Optional[str] = None,
    is_deletion: bool = False
):
    """Create a new content version record"""
    
    # Get next version number
    current_version = await get_latest_version_number(db, content_type, github_id)
    new_version = current_version + 1
    
    # Get previous version ID for linking
    previous_id = await get_previous_version_id(db, content_type, github_id, new_version)
    
    # Compute checksum
    checksum = compute_content_checksum(title, body, metadata)
    
    version = ContentVersion(
        webhook_event_id=webhook_event.id,
        content_type=content_type,
        github_id=github_id,
        github_node_id=github_node_id,
        version_number=new_version,
        previous_version_id=previous_id,
        is_deletion=is_deletion,
        title=title,
        body=body,
        state=state,
        metadata=metadata,
        actor_login=actor_login,
        actor_id=actor_id,
        github_created_at=parse_datetime(github_created_at),
        github_updated_at=parse_datetime(github_updated_at),
        checksum=checksum
    )
    
    db.add(version)
    logger.debug(f"Created {content_type} version {new_version} for github_id={github_id}")


async def process_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    event_type: str,
    action: Optional[str]
):
    """
    Process a webhook event and extract content versions.
    Routes to appropriate handler based on event type.
    """
    
    sender = payload.get("sender", {})
    actor_login = sender.get("login", "unknown")
    actor_id = sender.get("id", 0)
    
    try:
        if event_type == "issues":
            await process_issue_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "issue_comment":
            await process_issue_comment_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "pull_request":
            await process_pull_request_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "pull_request_review":
            await process_pr_review_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "pull_request_review_comment":
            await process_pr_review_comment_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "pull_request_review_thread":
            await process_pr_review_thread_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "discussion":
            await process_discussion_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "discussion_comment":
            await process_discussion_comment_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "release":
            await process_release_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "milestone":
            await process_milestone_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "gollum":
            await process_wiki_event(db, webhook_event, payload, actor_login, actor_id)
        
        elif event_type == "projects_v2":
            await process_project_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type == "projects_v2_item":
            await process_project_item_event(db, webhook_event, payload, action, actor_login, actor_id)
        
        elif event_type in ("team", "membership", "organization", "repository", "label"):
            # These are metadata events - store but no content extraction needed
            logger.debug(f"Stored metadata event: {event_type}.{action}")
        
        else:
            logger.debug(f"Unhandled event type: {event_type}")
    
    except Exception as e:
        logger.error(f"Error processing {event_type}.{action}: {e}")
        raise


# =============================================================================
# EVENT HANDLERS
# =============================================================================

async def process_issue_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process issue events"""
    issue = payload.get("issue", {})
    if not issue:
        return
    
    is_deletion = action == "deleted"
    
    # Extract labels, assignees, etc. as metadata
    metadata = {
        "labels": [l.get("name") for l in issue.get("labels", [])],
        "assignees": [a.get("login") for a in issue.get("assignees", [])],
        "milestone": issue.get("milestone", {}).get("title") if issue.get("milestone") else None,
        "url": issue.get("html_url"),
        "number": issue.get("number"),
        "locked": issue.get("locked"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="issue",
        github_id=issue.get("id"),
        title=issue.get("title"),
        body=issue.get("body"),
        state=issue.get("state"),
        metadata=metadata,
        actor_login=actor_login,
        actor_id=actor_id,
        github_node_id=issue.get("node_id"),
        github_created_at=issue.get("created_at"),
        github_updated_at=issue.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_issue_comment_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process issue comment events (also covers PR comments via issue API)"""
    comment = payload.get("comment", {})
    issue = payload.get("issue", {})
    if not comment:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "issue_id": issue.get("id"),
        "issue_number": issue.get("number"),
        "issue_title": issue.get("title"),
        "url": comment.get("html_url"),
        "author_association": comment.get("author_association"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="issue_comment",
        github_id=comment.get("id"),
        title=None,
        body=comment.get("body"),
        state=None,
        metadata=metadata,
        actor_login=comment.get("user", {}).get("login", actor_login),
        actor_id=comment.get("user", {}).get("id", actor_id),
        github_node_id=comment.get("node_id"),
        github_created_at=comment.get("created_at"),
        github_updated_at=comment.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_pull_request_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process pull request events"""
    pr = payload.get("pull_request", {})
    if not pr:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "labels": [l.get("name") for l in pr.get("labels", [])],
        "assignees": [a.get("login") for a in pr.get("assignees", [])],
        "reviewers": [r.get("login") for r in pr.get("requested_reviewers", [])],
        "milestone": pr.get("milestone", {}).get("title") if pr.get("milestone") else None,
        "url": pr.get("html_url"),
        "number": pr.get("number"),
        "draft": pr.get("draft"),
        "merged": pr.get("merged"),
        "merged_by": pr.get("merged_by", {}).get("login") if pr.get("merged_by") else None,
        "base_ref": pr.get("base", {}).get("ref"),
        "head_ref": pr.get("head", {}).get("ref"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="pull_request",
        github_id=pr.get("id"),
        title=pr.get("title"),
        body=pr.get("body"),
        state=pr.get("state"),
        metadata=metadata,
        actor_login=actor_login,
        actor_id=actor_id,
        github_node_id=pr.get("node_id"),
        github_created_at=pr.get("created_at"),
        github_updated_at=pr.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_pr_review_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process pull request review events"""
    review = payload.get("review", {})
    pr = payload.get("pull_request", {})
    if not review:
        return
    
    is_deletion = action == "dismissed"
    
    metadata = {
        "pull_request_id": pr.get("id"),
        "pull_request_number": pr.get("number"),
        "pull_request_title": pr.get("title"),
        "url": review.get("html_url"),
        "author_association": review.get("author_association"),
        "commit_id": review.get("commit_id"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="pull_request_review",
        github_id=review.get("id"),
        title=None,
        body=review.get("body"),
        state=review.get("state"),  # approved, changes_requested, commented
        metadata=metadata,
        actor_login=review.get("user", {}).get("login", actor_login),
        actor_id=review.get("user", {}).get("id", actor_id),
        github_node_id=review.get("node_id"),
        github_created_at=review.get("submitted_at"),
        github_updated_at=review.get("submitted_at"),
        is_deletion=is_deletion
    )


async def process_pr_review_comment_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process pull request review comment events (inline comments)"""
    comment = payload.get("comment", {})
    pr = payload.get("pull_request", {})
    if not comment:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "pull_request_id": pr.get("id"),
        "pull_request_number": pr.get("number"),
        "url": comment.get("html_url"),
        "path": comment.get("path"),
        "position": comment.get("position"),
        "original_position": comment.get("original_position"),
        "line": comment.get("line"),
        "original_line": comment.get("original_line"),
        "side": comment.get("side"),
        "diff_hunk": comment.get("diff_hunk"),
        "commit_id": comment.get("commit_id"),
        "original_commit_id": comment.get("original_commit_id"),
        "in_reply_to_id": comment.get("in_reply_to_id"),
        "author_association": comment.get("author_association"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="pull_request_review_comment",
        github_id=comment.get("id"),
        title=None,
        body=comment.get("body"),
        state=None,
        metadata=metadata,
        actor_login=comment.get("user", {}).get("login", actor_login),
        actor_id=comment.get("user", {}).get("id", actor_id),
        github_node_id=comment.get("node_id"),
        github_created_at=comment.get("created_at"),
        github_updated_at=comment.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_pr_review_thread_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process pull request review thread events"""
    thread = payload.get("thread", {})
    pr = payload.get("pull_request", {})
    if not thread:
        return
    
    metadata = {
        "pull_request_id": pr.get("id"),
        "pull_request_number": pr.get("number"),
        "line": thread.get("line"),
        "node_id": thread.get("node_id"),
        "action": action  # resolved, unresolved
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="pull_request_review_thread",
        github_id=thread.get("id"),
        title=None,
        body=None,
        state=action,  # resolved/unresolved
        metadata=metadata,
        actor_login=actor_login,
        actor_id=actor_id,
        github_node_id=thread.get("node_id")
    )


async def process_discussion_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process discussion events"""
    discussion = payload.get("discussion", {})
    if not discussion:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "url": discussion.get("html_url"),
        "number": discussion.get("number"),
        "category": discussion.get("category", {}).get("name"),
        "category_emoji": discussion.get("category", {}).get("emoji"),
        "locked": discussion.get("locked"),
        "answer_html_url": discussion.get("answer_html_url"),
        "answer_chosen_at": discussion.get("answer_chosen_at"),
        "labels": [l.get("name") for l in discussion.get("labels", [])],
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="discussion",
        github_id=discussion.get("id"),
        title=discussion.get("title"),
        body=discussion.get("body"),
        state=discussion.get("state"),
        metadata=metadata,
        actor_login=discussion.get("user", {}).get("login", actor_login),
        actor_id=discussion.get("user", {}).get("id", actor_id),
        github_node_id=discussion.get("node_id"),
        github_created_at=discussion.get("created_at"),
        github_updated_at=discussion.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_discussion_comment_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process discussion comment events"""
    comment = payload.get("comment", {})
    discussion = payload.get("discussion", {})
    if not comment:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "discussion_id": discussion.get("id"),
        "discussion_number": discussion.get("number"),
        "discussion_title": discussion.get("title"),
        "url": comment.get("html_url"),
        "parent_id": comment.get("parent_id"),
        "author_association": comment.get("author_association"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="discussion_comment",
        github_id=comment.get("id"),
        title=None,
        body=comment.get("body"),
        state=None,
        metadata=metadata,
        actor_login=comment.get("user", {}).get("login", actor_login),
        actor_id=comment.get("user", {}).get("id", actor_id),
        github_node_id=comment.get("node_id"),
        github_created_at=comment.get("created_at"),
        github_updated_at=comment.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_release_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process release events"""
    release = payload.get("release", {})
    if not release:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "url": release.get("html_url"),
        "tag_name": release.get("tag_name"),
        "target_commitish": release.get("target_commitish"),
        "draft": release.get("draft"),
        "prerelease": release.get("prerelease"),
        "assets": [{"name": a.get("name"), "url": a.get("browser_download_url")} 
                   for a in release.get("assets", [])],
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="release",
        github_id=release.get("id"),
        title=release.get("name"),
        body=release.get("body"),
        state="draft" if release.get("draft") else "published",
        metadata=metadata,
        actor_login=release.get("author", {}).get("login", actor_login),
        actor_id=release.get("author", {}).get("id", actor_id),
        github_node_id=release.get("node_id"),
        github_created_at=release.get("created_at"),
        github_updated_at=release.get("published_at"),
        is_deletion=is_deletion
    )


async def process_milestone_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process milestone events"""
    milestone = payload.get("milestone", {})
    if not milestone:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "url": milestone.get("html_url"),
        "number": milestone.get("number"),
        "due_on": milestone.get("due_on"),
        "open_issues": milestone.get("open_issues"),
        "closed_issues": milestone.get("closed_issues"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="milestone",
        github_id=milestone.get("id"),
        title=milestone.get("title"),
        body=milestone.get("description"),
        state=milestone.get("state"),
        metadata=metadata,
        actor_login=milestone.get("creator", {}).get("login", actor_login),
        actor_id=milestone.get("creator", {}).get("id", actor_id),
        github_node_id=milestone.get("node_id"),
        github_created_at=milestone.get("created_at"),
        github_updated_at=milestone.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_wiki_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    actor_login: str,
    actor_id: int
):
    """Process wiki (gollum) events"""
    pages = payload.get("pages", [])
    
    for page in pages:
        metadata = {
            "page_name": page.get("page_name"),
            "html_url": page.get("html_url"),
            "action": page.get("action"),  # created, edited
            "sha": page.get("sha")
        }
        
        # Wiki pages don't have a numeric ID, so we create one from the title
        # This uses a hash to ensure consistency across edits
        page_id_str = f"wiki:{payload.get('repository', {}).get('id')}:{page.get('page_name')}"
        github_id = abs(hash(page_id_str)) % (10 ** 18)  # Positive bigint
        
        await create_content_version(
            db, webhook_event,
            content_type="wiki_page",
            github_id=github_id,
            title=page.get("title"),
            body=page.get("summary"),  # Wiki only sends summary in webhook, not full content
            state=page.get("action"),
            metadata=metadata,
            actor_login=actor_login,
            actor_id=actor_id
        )


async def process_project_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process projects_v2 events"""
    project = payload.get("projects_v2", {})
    if not project:
        return
    
    is_deletion = action == "deleted"
    
    metadata = {
        "url": project.get("html_url"),
        "number": project.get("number"),
        "public": project.get("public"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="project",
        github_id=project.get("id"),
        title=project.get("title"),
        body=project.get("short_description"),
        state="closed" if project.get("closed") else "open",
        metadata=metadata,
        actor_login=actor_login,
        actor_id=actor_id,
        github_node_id=project.get("node_id"),
        github_created_at=project.get("created_at"),
        github_updated_at=project.get("updated_at"),
        is_deletion=is_deletion
    )


async def process_project_item_event(
    db: AsyncSession,
    webhook_event: WebhookEvent,
    payload: dict,
    action: str,
    actor_login: str,
    actor_id: int
):
    """Process projects_v2_item events"""
    item = payload.get("projects_v2_item", {})
    if not item:
        return
    
    is_deletion = action in ("deleted", "archived")
    
    metadata = {
        "project_node_id": item.get("project_node_id"),
        "content_type": item.get("content_type"),
        "content_node_id": item.get("content_node_id"),
        "archived_at": item.get("archived_at"),
        "action": action
    }
    
    await create_content_version(
        db, webhook_event,
        content_type="project_item",
        github_id=item.get("id"),
        title=None,
        body=None,
        state="archived" if item.get("archived_at") else "active",
        metadata=metadata,
        actor_login=actor_login,
        actor_id=actor_id,
        github_node_id=item.get("node_id"),
        github_created_at=item.get("created_at"),
        github_updated_at=item.get("updated_at"),
        is_deletion=is_deletion
    )
