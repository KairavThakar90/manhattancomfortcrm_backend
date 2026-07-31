from sqlalchemy.orm import Session
from app import models
import uuid

def log_activity(
    db: Session, 
    action: str, 
    user_id: uuid.UUID = None, 
    entity_type: str = None, 
    entity_id: str = None, 
    details: dict = None
):
    """
    Utility function to quickly log user or system activity.
    """
    try:
        log_entry = models.UserActivityLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            details=details
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Failed to log activity '{action}': {e}")
