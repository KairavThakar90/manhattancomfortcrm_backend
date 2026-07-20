from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import VendorOut, PaginatedResponse, SyncResponse
from app.services.sync_service import sync_vendors

router = APIRouter(prefix="/vendors", tags=["Vendors"], dependencies=[Depends(get_current_user)])


class UpdateVendorLeadTime(BaseModel):
    container_lead_time_days: int


@router.get("", response_model=PaginatedResponse)
def list_vendors(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    db: Session = Depends(get_db),
):
    """List all vendors with pagination."""
    q = db.query(models.Vendor)
    if is_active is not None:
        q = q.filter(models.Vendor.is_active == is_active)

    total = q.count()
    rows = (
        q.order_by(models.Vendor.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[VendorOut.model_validate(r) for r in rows],
    )


@router.get("/{vendor_id}", response_model=VendorOut)
def get_vendor(vendor_id: str, db: Session = Depends(get_db)):
    """Get a specific vendor by ID."""
    vendor = (
        db.query(models.Vendor)
        .filter(models.Vendor.id == vendor_id)
        .first()
    )
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.patch("/{vendor_id}/lead-time", response_model=VendorOut)
def update_vendor_lead_time(
    vendor_id: str,
    update: UpdateVendorLeadTime,
    db: Session = Depends(get_db)
):
    """
    Update the container lead time (in days) for a specific vendor.
    This is used to calculate when the first container should arrive after payment/invoice.
    
    Example: If a vendor's lead time is 45 days, and the invoice date is Jan 1,
    the first container is expected to arrive by Feb 15.
    """
    vendor = db.query(models.Vendor).filter(models.Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    
    vendor.container_lead_time_days = update.container_lead_time_days
    db.commit()
    db.refresh(vendor)
    
    return vendor


@router.post("/sync", response_model=SyncResponse)
def trigger_sync(db: Session = Depends(get_db)):
    """
    Pulls latest Vendors from SellerCloud into Neon.
    This will update existing vendor records (created as stubs during PO sync)
    with full vendor details including name, email, phone, address, etc.
    """
    count = sync_vendors(db)
    return SyncResponse(entity_type="vendors", status="success", records_synced=count)
