from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/activities", tags=["Activities"])

@router.get("", response_model=schemas.PaginatedResponse)
def get_activities(
    user_id: Optional[uuid.UUID] = Query(None, description="Filter logs by a specific user"),
    action: Optional[str] = Query(None, description="Filter by action type (e.g. LOGIN)"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Fetch a paginated list of activity logs.
    """
    query = db.query(models.UserActivityLog)
    
    if user_id:
        query = query.filter(models.UserActivityLog.user_id == user_id)
    
    if action:
        query = query.filter(models.UserActivityLog.action == action)
        
    total_count = query.count()
    total_pages = (total_count + size - 1) // size
    
    logs = (
        query.order_by(models.UserActivityLog.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    
    # Enrich with user name
    result_data = []
    for log in logs:
        log_out = schemas.UserActivityLogOut.model_validate(log)
        if log.user:
            log_out.user_name = log.user.full_name or log.user.email
        result_data.append(log_out)
        
    return {
        "items": result_data,
        "total": total_count,
        "page": page,
        "size": size,
        "pages": total_pages
    }

@router.post("", response_model=dict)
def create_activity(
    activity: schemas.UserActivityLogCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Log a custom user activity from the frontend (e.g. clicking a button, viewing a page).
    """
    from app.services.activity_service import log_activity
    log_activity(
        db=db,
        action=activity.action,
        user_id=current_user.id,
        entity_type=activity.entity_type,
        entity_id=activity.entity_id,
        details=activity.details
    )
    return {"status": "success", "message": "Activity logged"}

