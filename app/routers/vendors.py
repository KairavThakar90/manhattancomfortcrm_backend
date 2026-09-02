import uuid
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
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    db: Session = Depends(get_db),
):
    """List all vendors with pagination."""
    from sqlalchemy import func
    
    q = db.query(
        models.Vendor,
        func.count(models.PurchaseOrder.id).label('po_count')
    ).outerjoin(
        models.PurchaseOrder, models.Vendor.id == models.PurchaseOrder.vendor_id
    )
    
    if is_active is not None:
        q = q.filter(models.Vendor.is_active == is_active)

    q = q.group_by(models.Vendor.id)

    total = q.count()
    if page and page_size:
        rows = (
            q.order_by(models.Vendor.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
    else:
        rows = q.order_by(models.Vendor.name).all()
    
    results = []
    for vendor, po_count in rows:
        vendor_dict = VendorOut.model_validate(vendor).model_dump(mode='python')
        vendor_dict['po_count'] = po_count
        results.append(vendor_dict)
        
    return PaginatedResponse(
        total=total,
        page=page if page else 1,
        page_size=page_size if page_size else total,
        results=results,
    )


@router.get("/me", response_model=list[VendorOut])
def get_my_vendors(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all vendors assigned to the currently logged in user."""
    effective_ids = current_user.effective_vendor_ids
    if not effective_ids:
        return []

    from sqlalchemy import func
    rows = db.query(
        models.Vendor,
        func.count(models.PurchaseOrder.id).label('po_count')
    ).outerjoin(
        models.PurchaseOrder, models.Vendor.id == models.PurchaseOrder.vendor_id
    ).filter(
        models.Vendor.id.in_(effective_ids)
    ).group_by(models.Vendor.id).order_by(models.Vendor.name).all()

    results = []
    for vendor, po_count in rows:
        v_dict = VendorOut.model_validate(vendor).model_dump(mode='python')
        v_dict['po_count'] = po_count
        results.append(v_dict)

    return results


@router.get("/user/{user_id}", response_model=list[VendorOut])
def get_vendors_for_user(
    user_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all vendors assigned to a specific user (admin or self)."""
    try:
        user_uuid = uuid.UUID(str(user_id).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid user ID format. Must be a valid UUID.")

    if current_user.role != "admin" and current_user.id != user_uuid:
        raise HTTPException(status_code=403, detail="Not authorized. Admin or own account only.")

    user = db.query(models.User).filter(models.User.id == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    effective_ids = user.effective_vendor_ids
    if not effective_ids:
        return []

    from sqlalchemy import func
    rows = db.query(
        models.Vendor,
        func.count(models.PurchaseOrder.id).label('po_count')
    ).outerjoin(
        models.PurchaseOrder, models.Vendor.id == models.PurchaseOrder.vendor_id
    ).filter(
        models.Vendor.id.in_(effective_ids)
    ).group_by(models.Vendor.id).order_by(models.Vendor.name).all()

    results = []
    for vendor, po_count in rows:
        v_dict = VendorOut.model_validate(vendor).model_dump(mode='python')
        v_dict['po_count'] = po_count
        results.append(v_dict)

    return results


@router.get("/{vendor_id}", response_model=VendorOut)
def get_vendor(vendor_id: str, db: Session = Depends(get_db)):
    """Get a specific vendor by ID."""
    try:
        v_uuid = uuid.UUID(str(vendor_id).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid vendor ID format. Must be a valid UUID.")

    vendor = (
        db.query(models.Vendor)
        .filter(models.Vendor.id == v_uuid)
        .first()
    )
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


from app.schemas import VendorUpdate

@router.patch("/{vendor_id}", response_model=VendorOut)
def update_vendor(
    vendor_id: str,
    update: VendorUpdate,
    db: Session = Depends(get_db)
):
    """
    Update vendor information.
    Allows updating name, country, phone, payment_terms, and lead_time.
    """
    try:
        v_uuid = uuid.UUID(str(vendor_id).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid vendor ID format. Must be a valid UUID.")

    vendor = db.query(models.Vendor).filter(models.Vendor.id == v_uuid).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    
    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(vendor, key, value)
        
    db.commit()
    db.refresh(vendor)
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
    try:
        count = sync_vendors(db)
        return SyncResponse(
            success=True, 
            message="Vendors synced successfully", 
            records_synced=count, 
            entity_type="vendors", 
            status="success"
        )
    except Exception as e:
        return SyncResponse(
            success=False, 
            message="Error syncing vendors", 
            error=str(e), 
            entity_type="vendors", 
            status="error"
        )
