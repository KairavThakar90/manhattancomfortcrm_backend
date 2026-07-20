from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import PurchaseOrderOut, PaginatedResponse, SyncResponse
from app.services.sync_service import sync_purchase_orders, sync_containers

router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=PaginatedResponse)
def list_purchase_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    status_code: Optional[int] = Query(None, description="Raw SellerCloud PurchaseOrderStatus code"),
    vendor_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.PurchaseOrder).options(
        joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
        joinedload(models.PurchaseOrder.vendor)
    )
    if status_code is not None:
        q = q.filter(models.PurchaseOrder.purchase_order_status_code == status_code)
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)

    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.date_ordered.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[PurchaseOrderOut.model_validate(r) for r in rows],
    )


@router.get("/{po_id}", response_model=PurchaseOrderOut)
def get_purchase_order(po_id: str, db: Session = Depends(get_db)):
    po = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(models.PurchaseOrder.id == po_id)
        .first()
    )
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


@router.get("/flags/missing-invoice", response_model=PaginatedResponse)
def get_pos_missing_invoice(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    days_threshold: int = Query(10, ge=1, description="Number of days after creation to flag PO without invoice"),
    db: Session = Depends(get_db),
):
    """
    Get purchase orders that don't have an invoice date after X days (default 10).
    This helps identify POs that need follow-up for payment/invoice.
    
    Flags POs where:
    - invoice_date is NULL
    - created_on is more than X days ago
    """
    cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)
    
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_date
            )
        )
    )
    
    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.created_on.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[PurchaseOrderOut.model_validate(r) for r in rows],
    )


@router.get("/flags/overdue-containers", response_model=PaginatedResponse)
def get_overdue_containers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Get purchase orders where the first container is overdue based on:
    - invoice_date + vendor's container_lead_time_days
    
    This flags POs where:
    - invoice_date exists
    - vendor has container_lead_time_days set
    - (invoice_date + lead_time_days) < today
    - receiving_status_code indicates not fully received (you may need to adjust this filter)
    
    Example: If invoice date is Jan 1 and vendor lead time is 45 days,
    the container is expected by Feb 15. If today is Feb 20, this PO is flagged.
    """
    today = datetime.utcnow().date()
    
    # Get all POs with invoice dates and vendor lead times
    q = (
        db.query(models.PurchaseOrder)
        .join(models.Vendor)
        .options(
            joinedload(models.PurchaseOrder.items),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.Vendor.container_lead_time_days.isnot(None),
                # Optionally filter by receiving status to only show not-fully-received POs
                # Adjust the status code based on your SellerCloud enum
                # For example: models.PurchaseOrder.receiving_status_code != FULLY_RECEIVED_CODE
            )
        )
    )
    
    # Filter in Python since we need to calculate invoice_date + lead_time
    overdue_pos = []
    for po in q.all():
        if po.invoice_date and po.vendor and po.vendor.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.vendor.container_lead_time_days)
            if expected_arrival < today:
                overdue_pos.append(po)
    
    # Sort by how overdue they are (oldest expected arrival first)
    overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.vendor.container_lead_time_days))
    
    # Paginate
    total = len(overdue_pos)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = overdue_pos[start:end]
    
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[PurchaseOrderOut.model_validate(r) for r in paginated_pos],
    )


@router.post("/sync", response_model=SyncResponse)
def trigger_sync(
    view_id: Optional[int] = Query(None, description="SellerCloud saved PO view ID, defaults to 25"),
    db: Session = Depends(get_db),
):
    """Pulls latest Purchase Orders (+ line items) from SellerCloud into Neon."""
    count = sync_purchase_orders(db, view_id=view_id)
    return SyncResponse(entity_type="purchase_orders", status="success", records_synced=count)


@router.post("/{sellercloud_po_id}/sync-containers")
def trigger_container_sync(sellercloud_po_id: int, db: Session = Depends(get_db)):
    """
    Discovers and syncs shipping container data for one PO's items (container
    name, ETA, received date, and per-item quantities). Scoped to a single PO
    to keep the SellerCloud API call volume reasonable - a container fetch
    triggered here may also backfill links for OTHER already-synced POs that
    share the same consolidated container.
    """
    result = sync_containers(db, po_id=sellercloud_po_id)
    return {
        "sellercloud_po_id": sellercloud_po_id,
        "containers_synced": result["containers_synced"],
        "links_synced": result["links_synced"],
    }
