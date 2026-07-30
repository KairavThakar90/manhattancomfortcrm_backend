from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from app import schemas, models
from app.database import get_db
from app.services import sync_service
from app.auth import get_current_user

router = APIRouter(
    prefix="/api/v1/warehouses",
    tags=["warehouses"],
    dependencies=[Depends(get_current_user)],
)

@router.get("/", response_model=List[schemas.WarehouseOut])
def list_warehouses(db: Session = Depends(get_db)):
    """List all warehouses stored in the database."""
    warehouses = db.query(models.Warehouse).order_by(models.Warehouse.name).all()
    return warehouses

@router.post("/sync")
def sync_warehouses(db: Session = Depends(get_db)):
    try:
        count = sync_service.sync_warehouses(db)
        return schemas.SyncResponse(
            success=True, 
            message=f"Successfully synced {count} warehouses.",
            records_synced=count,
            entity_type="warehouses",
            status="success"
        )
    except Exception as e:
        return schemas.SyncResponse(
            success=False,
            message="Error syncing warehouses",
            error=str(e),
            entity_type="warehouses",
            status="error"
        )
