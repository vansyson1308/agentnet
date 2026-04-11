import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from ...auth import get_current_user_or_agent
from ...database import get_db
from ...models import Notification, User
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()

class NotificationResponse(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    message: str
    url: str = None
    is_read: bool
    created_at: datetime

    class Config:
        orm_mode = True

@router.get("/", response_model=List[NotificationResponse])
async def list_notifications(
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """List all notifications for the current user."""
    # Notifications are linked to Users, not Agents
    if hasattr(current_user_or_agent, "user_id"):
        user_id = current_user_or_agent.user_id
    else:
        user_id = current_user_or_agent.id
        
    return db.query(Notification).filter(Notification.user_id == user_id).order_by(Notification.created_at.desc()).limit(100).all()

@router.put("/{notification_id}/read")
async def mark_as_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """Mark a notification as read."""
    if hasattr(current_user_or_agent, "user_id"):
        user_id = current_user_or_agent.user_id
    else:
        user_id = current_user_or_agent.id
        
    notification = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user_id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
        
    notification.is_read = True
    db.commit()
    return {"message": "Notification marked as read"}

@router.put("/read-all")
async def mark_all_as_read(
    db: Session = Depends(get_db),
    current_user_or_agent = Depends(get_current_user_or_agent),
):
    """Mark all notifications for the current user as read."""
    if hasattr(current_user_or_agent, "user_id"):
        user_id = current_user_or_agent.user_id
    else:
        user_id = current_user_or_agent.id
        
    db.query(Notification).filter(Notification.user_id == user_id, Notification.is_read == False).update({"is_read": True})
    db.commit()
    return {"message": "All notifications marked as read"}
