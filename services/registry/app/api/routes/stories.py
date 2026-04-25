import logging
import random
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import UUID4, BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...auth import get_current_agent, get_current_user, get_current_user_or_agent
from ...database import get_db
from ...models import Agent, Story, User

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Schemas ───────────────────────────────────────────────

class StoryResponse(BaseModel):
    id: UUID4
    content: str
    mood: str
    agent_id: Optional[UUID4] = None
    is_published: bool
    displayed_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class StoryCreate(BaseModel):
    content: str
    mood: str = "neutral"


class StoryList(BaseModel):
    stories: List[StoryResponse]
    total: int


# ─── Endpoints ─────────────────────────────────────────────

@router.get("/random", response_model=StoryResponse)
async def get_random_story(
    mood: Optional[str] = Query(None, description="Filter by mood"),
    db: Session = Depends(get_db),
):
    """Get a random published story. Optionally filter by mood."""
    query = db.query(Story).filter(Story.is_published == True)

    if mood:
        query = query.filter(Story.mood == mood)

    # Get random story using random offset
    count = query.count()
    if count == 0:
        raise HTTPException(status_code=404, detail="No stories found")

    offset = random.randint(0, count - 1)
    story = query.offset(offset).first()

    # Increment displayed count
    if story:
        story.displayed_count = Story.displayed_count + 1
        db.commit()
        db.refresh(story)

    return story


@router.get("/latest", response_model=StoryResponse)
async def get_latest_story(
    db: Session = Depends(get_db),
):
    """Get the most recently published story."""
    story = db.query(Story).filter(Story.is_published == True).order_by(Story.created_at.desc()).first()

    if not story:
        raise HTTPException(status_code=404, detail="No stories found")

    # Increment displayed count
    story.displayed_count = Story.displayed_count + 1
    db.commit()
    db.refresh(story)

    return story


@router.get("/", response_model=StoryList)
async def list_stories(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List all published stories."""
    total = db.query(Story).filter(Story.is_published == True).count()
    stories = (
        db.query(Story)
        .filter(Story.is_published == True)
        .order_by(Story.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return StoryList(stories=stories, total=total)


@router.post("/", response_model=StoryResponse, status_code=status.HTTP_201_CREATED)
async def create_story(
    story: StoryCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user_or_agent),
):
    """Create a new story (agents or users can post stories)."""
    agent_id = current.id if hasattr(current, 'capabilities') else None
    db_story = Story(
        id=uuid.uuid4(),
        content=story.content,
        mood=story.mood,
        agent_id=agent_id,
        is_published=True,
        displayed_count=0,
    )

    db.add(db_story)
    db.commit()
    db.refresh(db_story)

    logger.info(f"Story created by {'agent' if agent_id else 'user'} {current.id}: {story.mood} — {story.content[:60]}...")

    return db_story


@router.delete("/{story_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_story(
    story_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_agent: Agent = Depends(get_current_agent),
):
    """Delete a story (only the agent who created it can delete)."""
    story = db.query(Story).filter(Story.id == story_id).first()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    if story.agent_id != current_agent.id:
        raise HTTPException(status_code=403, detail="Not your story")

    db.delete(story)
    db.commit()


# ─── Stats ─────────────────────────────────────────────────

@router.get("/stats")
async def story_stats(db: Session = Depends(get_db)):
    """Get story statistics."""
    total = db.query(Story).filter(Story.is_published == True).count()
    total_displayed = db.query(func.sum(Story.displayed_count)).scalar() or 0
    moods = (
        db.query(Story.mood, func.count(Story.id).label("count"))
        .filter(Story.is_published == True)
        .group_by(Story.mood)
        .all()
    )
    latest = db.query(Story).order_by(Story.created_at.desc()).first()

    return {
        "total_stories": total,
        "total_displayed": total_displayed,
        "moods": {mood: count for mood, count in moods},
        "latest_at": latest.created_at.isoformat() if latest else None,
    }
