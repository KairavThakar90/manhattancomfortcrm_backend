from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/containers", tags=["Containers"], dependencies=[Depends(get_current_user)])


@router.get("")
def list_containers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(models.ShippingContainer).order_by(models.ShippingContainer.created_at.desc())

    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()

    results = []
    for container in rows:
        results.append(
            {
                "id": str(container.id),
                "sellercloud_container_id": container.sellercloud_container_id,
                "container_name": container.container_name,
                "estimated_arrival_date": container.estimated_arrival_date,
                "received_date": container.received_date,
                "item_count": len(container.item_links),
                "created_at": container.created_at,
                "updated_at": container.updated_at,
            }
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1,
        },
        "results": results,
    }
