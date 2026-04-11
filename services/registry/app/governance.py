import uuid
from typing import Optional
from sqlalchemy.orm import Session
from .models import Notification

def create_notification(
    db: Session,
    user_id: uuid.UUID,
    type: str,
    title: str,
    message: str,
    url: Optional[str] = None
):
    """Create a persistent notification for a user."""
    notification = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        type=type,
        title=title,
        message=message,
        url=url
    )
    db.add(notification)
    db.commit()
    return notification
