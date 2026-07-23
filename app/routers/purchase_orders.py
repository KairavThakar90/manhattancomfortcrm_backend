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


@router.get("/summary/status-counts")
def get_status_counts(db: Session = Depends(get_db)):
    """
    Get counts of POs by status flags.
    
    Returns:
    - total_pos: Total number of POs
    - delayed_invoice_count: Count of POs with delayed invoices
    - overdue_container_count: Count of POs with overdue containers
    - both_issues_count: Count of POs with both issues
    
    This is more efficient than fetching all POs and counting in frontend.
    """
    from datetime import datetime, timedelta, timezone
    
    # Get all POs
    pos = db.query(models.PurchaseOrder).all()
    
    today = datetime.now(timezone.utc).date()
    
    total_pos = len(pos)
    delayed_invoice_count = 0
    overdue_container_count = 0
    both_issues_count = 0
    
    for po in pos:
        is_invoice_delayed = False
        is_container_overdue = False
        
        # Check invoice delayed
        if po.invoice_date:
            is_invoice_delayed = False
        elif po.created_on:
            days_since_creation = (today - po.created_on.date()).days
            is_invoice_delayed = days_since_creation > 10
        
        # Check container overdue based on PO lead time
        if po.invoice_date and po.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
            is_container_overdue = expected_arrival < today
        
        # Count
        if is_invoice_delayed:
            delayed_invoice_count += 1
        if is_container_overdue:
            overdue_container_count += 1
        if is_invoice_delayed and is_container_overdue:
            both_issues_count += 1
    
    return {
        "total_pos": total_pos,
        "delayed_invoice_count": delayed_invoice_count,
        "overdue_container_count": overdue_container_count,
        "both_issues_count": both_issues_count,
        "summary": {
            "delayed_invoice_percentage": round((delayed_invoice_count / total_pos * 100), 2) if total_pos > 0 else 0,
            "overdue_container_percentage": round((overdue_container_count / total_pos * 100), 2) if total_pos > 0 else 0,
        }
    }


@router.get("")
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
    
    # Convert to Pydantic models BEFORE converting to dicts
    # This ensures model_validate can access container_links
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    
    # Now convert to dicts
    results = [po.model_dump(mode='python') for po in po_models]
    
    # Build response with meta object
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
            "has_prev": page > 1
        },
        "results": results,
    }


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


@router.get("/filters/all")
def get_filtered_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    filter_type: Optional[str] = Query(None, description="Filter type: new_without_invoice, invoice_delayed, delivery_overdue, remaining_items"),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Unified filter endpoint for purchase orders.
    
    Filter Types:
    - new_without_invoice: POs created in last 10 days without invoice
    - invoice_delayed: POs older than 10 days without invoice
    - delivery_overdue: POs where delivery is overdue (invoice_date + lead_time < today)
    - remaining_items: POs with items not fully received (qty_remaining > 0)
    - If no filter_type provided, returns all POs
    
    Optional vendor_id parameter works with all filter types.
    
    Examples:
    - All POs: GET /api/v1/purchase-orders/filters/all
    - With filter: GET /api/v1/purchase-orders/filters/all?filter_type=delivery_overdue
    - With vendor: GET /api/v1/purchase-orders/filters/all?filter_type=remaining_items&vendor_id=xxx
    - Vendor only: GET /api/v1/purchase-orders/filters/all?vendor_id=xxx
    """
    from datetime import timezone
    
    cutoff_10_days = datetime.now(timezone.utc) - timedelta(days=10)
    today = datetime.now(timezone.utc).date()
    
    # Base query
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor)
        )
    )
    
    # Apply vendor filter if provided
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    # Apply filter type
    if filter_type == "new_without_invoice":
        # POs created in last 10 days without invoice
        q = q.filter(
            and_(
                models.PurchaseOrder.created_on >= cutoff_10_days,
                models.PurchaseOrder.invoice_date.is_(None)
            )
        )
        q = q.order_by(models.PurchaseOrder.created_on.desc())
        
        # Execute query and paginate
        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
        
    elif filter_type == "invoice_delayed":
        # POs older than 10 days without invoice
        q = q.filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_10_days
            )
        )
        q = q.order_by(models.PurchaseOrder.created_on.asc())
        
        # Execute query and paginate
        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
        
    elif filter_type == "delivery_overdue":
        # POs where delivery is overdue (requires calculation)
        q = q.filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None)
            )
        )
        
        # Get all matching POs and filter by calculation
        all_pos = q.all()
        overdue_pos = []
        for po in all_pos:
            if po.invoice_date and po.container_lead_time_days:
                expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
                if expected_arrival < today:
                    overdue_pos.append(po)
        
        # Sort by how overdue
        overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
        
        # Paginate
        total = len(overdue_pos)
        start = (page - 1) * page_size
        end = start + page_size
        rows = overdue_pos[start:end]
        
    elif filter_type == "remaining_items":
        # POs with remaining items (requires calculation)
        q = q.join(models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id)
        all_pos = q.distinct().all()
        
        # Filter POs with remaining items
        pos_with_remaining = []
        for po in all_pos:
            total_remaining = sum(
                item.qty_ordered - item.qty_received 
                for item in po.items
            )
            if total_remaining > 0:
                pos_with_remaining.append(po)
        
        # Sort by created_on descending
        pos_with_remaining.sort(key=lambda po: po.created_on or datetime.min, reverse=True)
        
        # Paginate
        total = len(pos_with_remaining)
        start = (page - 1) * page_size
        end = start + page_size
        rows = pos_with_remaining[start:end]
        
    else:
        # No filter - return all POs (with vendor filter if provided)
        q = q.order_by(models.PurchaseOrder.date_ordered.desc())
        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
    
    # Convert to response format
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    results = [po.model_dump(mode='python') for po in po_models]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "filter_type": filter_type or "all",
        "vendor_id": vendor_id,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/new-without-invoice")
def get_new_pos_without_invoice(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs that arrived in the last 10 days and have no invoice date.
    
    Filters:
    - created_on within last 10 days
    - invoice_date is NULL
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/new-without-invoice
    Example with vendor: GET /api/v1/purchase-orders/filters/new-without-invoice?vendor_id=xxx
    """
    from datetime import timezone
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=10)
    
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(
            and_(
                models.PurchaseOrder.created_on >= cutoff_date,
                models.PurchaseOrder.invoice_date.is_(None)
            )
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.created_on.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    results = [po.model_dump(mode='python') for po in po_models]
    
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
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/invoice-delayed")
def get_invoice_delayed_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs with delayed invoices (no invoice date after 10 days from creation).
    
    Filters:
    - invoice_date is NULL
    - created_on is more than 10 days ago
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/invoice-delayed
    Example with vendor: GET /api/v1/purchase-orders/filters/invoice-delayed?vendor_id=xxx
    """
    from datetime import timezone
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=10)
    
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_date
            )
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.created_on.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    results = [po.model_dump(mode='python') for po in po_models]
    
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
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/delivery-overdue")
def get_delivery_overdue_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs with overdue deliveries (container overdue based on invoice_date + PO lead time).
    
    Filters:
    - invoice_date exists
    - PO has container_lead_time_days set
    - (invoice_date + lead_time_days) < today
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/delivery-overdue
    Example with vendor: GET /api/v1/purchase-orders/filters/delivery-overdue?vendor_id=xxx
    """
    from datetime import timezone
    
    today = datetime.now(timezone.utc).date()
    
    # Get all POs with invoice dates and PO lead times
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None),
            )
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    # Filter in Python since we need to calculate invoice_date + lead_time
    overdue_pos = []
    for po in q.all():
        if po.invoice_date and po.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
            if expected_arrival < today:
                overdue_pos.append(po)
    
    # Sort by how overdue they are (oldest expected arrival first)
    overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
    
    # Paginate
    total = len(overdue_pos)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = overdue_pos[start:end]
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in paginated_pos]
    results = [po.model_dump(mode='python') for po in po_models]
    
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
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/remaining-items")
def get_pos_with_remaining_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs that have remaining items (qty_remaining > 0).
    
    Filters:
    - total_qty_remaining > 0 (items not fully received)
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/remaining-items
    Example with vendor: GET /api/v1/purchase-orders/filters/remaining-items?vendor_id=xxx
    """
    q = (
        db.query(models.PurchaseOrder)
        .join(models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor)
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    # Filter POs where at least one item has qty_ordered > qty_received
    # We need to check this in Python since it's calculated
    all_pos = q.distinct().all()
    
    pos_with_remaining = []
    for po in all_pos:
        total_remaining = sum(
            item.qty_ordered - item.qty_received 
            for item in po.items
        )
        if total_remaining > 0:
            pos_with_remaining.append(po)
    
    # Sort by created_on descending
    pos_with_remaining.sort(key=lambda po: po.created_on or datetime.min, reverse=True)
    
    # Paginate
    total = len(pos_with_remaining)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = pos_with_remaining[start:end]
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in paginated_pos]
    results = [po.model_dump(mode='python') for po in po_models]
    
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
            "has_prev": page > 1
        },
        "results": results,
    }


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
            "has_prev": page > 1
        },
        "results": [PurchaseOrderOut.model_validate(r).model_dump() for r in rows],
    }


@router.get("/flags/overdue-containers")
def get_overdue_containers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Get purchase orders where the first container is overdue based on:
    - invoice_date + PO's container_lead_time_days
    
    This flags POs where:
    - invoice_date exists
    - PO has container_lead_time_days set
    - (invoice_date + lead_time_days) < today
    
    Example: If invoice date is Jan 1 and PO lead time is 45 days,
    the container is expected by Feb 15. If today is Feb 20, this PO is flagged.
    """
    today = datetime.utcnow().date()
    
    # Get all POs with invoice dates and PO lead times
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items),
            joinedload(models.PurchaseOrder.vendor)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None),
            )
        )
    )
    
    # Filter in Python since we need to calculate invoice_date + lead_time
    overdue_pos = []
    for po in q.all():
        if po.invoice_date and po.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
            if expected_arrival < today:
                overdue_pos.append(po)
    
    # Sort by how overdue they are (oldest expected arrival first)
    overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
    
    # Paginate
    total = len(overdue_pos)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = overdue_pos[start:end]
    
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
            "has_prev": page > 1
        },
        "results": [PurchaseOrderOut.model_validate(r).model_dump() for r in paginated_pos],
    }


@router.patch("/{po_id}/lead-time")
def update_po_lead_time(
    po_id: str,
    container_lead_time_days: int = Query(..., description="Container lead time in days"),
    db: Session = Depends(get_db)
):
    """
    Update container lead time for a specific purchase order.
    
    Lead time is the number of days from invoice date to expected container arrival.
    This is set per PO, not per vendor, for more granular control.
    
    Example: 45 days means container arrives 45 days after invoice date.
    """
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
    
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    
    po.container_lead_time_days = container_lead_time_days
    db.commit()
    db.refresh(po)
    
    return {
        "message": "Lead time updated successfully",
        "po_id": str(po.id),
        "sellercloud_po_id": po.sellercloud_po_id,
        "container_lead_time_days": po.container_lead_time_days
    }


@router.post("/sync", response_model=SyncResponse)
def trigger_sync(
    view_id: Optional[int] = Query(None, description="SellerCloud saved PO view ID, defaults to 25"),
    db: Session = Depends(get_db),
):
    """Pulls latest Purchase Orders (+ line items) from SellerCloud into Neon."""
    count = sync_purchase_orders(db, view_id=view_id)
    return SyncResponse(entity_type="purchase_orders", status="success", records_synced=count)


@router.post("/{sellercloud_po_id}/sync")
def trigger_single_po_sync(
    sellercloud_po_id: int,
    db: Session = Depends(get_db)
):
    """
    Sync a specific purchase order by its SellerCloud PO ID.
    
    This will:
    1. Fetch the PO detail from SellerCloud
    2. Upsert the PO and its items into the database
    3. Update existing PO if it already exists
    
    Example: POST /api/v1/purchase-orders/12147/sync
    
    Returns:
    - sellercloud_po_id: The PO ID that was synced
    - status: success or error
    - message: Details about the sync
    """
    from app.services.sync_service import _map_po, _get_or_create_company, _get_or_create_vendor, _upsert_items
    from app.services.sellercloud_client import sellercloud_client
    
    try:
        # Fetch PO detail from SellerCloud
        detail = sellercloud_client.get_purchase_order_detail(sellercloud_po_id)
        
        if not detail:
            raise HTTPException(status_code=404, detail=f"PO {sellercloud_po_id} not found in SellerCloud")
        
        # Map the PO data
        mapped = _map_po(detail)
        
        # Get or create related entities
        purchase = detail.get("Purchase") or {}
        company_sc_id = purchase.get("CompanyId")
        vendor_sc_id = purchase.get("VendorId")
        
        company = _get_or_create_company(db, company_sc_id)
        vendor = _get_or_create_vendor(db, vendor_sc_id)
        
        # Upsert the PO
        existing_po = (
            db.query(models.PurchaseOrder)
            .filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id)
            .first()
        )
        
        if existing_po:
            # Update existing PO
            for key, val in mapped.items():
                setattr(existing_po, key, val)
            existing_po.company_id = company.id if company else None
            existing_po.vendor_id = vendor.id if vendor else None
            po_row = existing_po
            
            # Delete existing items to re-create them
            db.query(models.PurchaseOrderItem).filter(
                models.PurchaseOrderItem.purchase_order_id == existing_po.id
            ).delete()
        else:
            # Create new PO
            po_row = models.PurchaseOrder(
                **mapped,
                company_id=company.id if company else None,
                vendor_id=vendor.id if vendor else None
            )
            db.add(po_row)
        
        db.flush()  # Get po_row.id
        
        # Upsert items
        items = detail.get("Items") or []
        _upsert_items(db, po_row.id, items)
        
        db.commit()
        
        return {
            "sellercloud_po_id": sellercloud_po_id,
            "status": "success",
            "message": f"Successfully synced PO {sellercloud_po_id}",
            "items_count": len(items)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error syncing PO: {str(e)}")


@router.post("/sync-containers")
def trigger_all_containers_sync(
    limit: Optional[int] = Query(None, description="Limit number of POs to sync (for testing)"),
    db: Session = Depends(get_db)
):
    """
    Sync shipping container data for ALL purchase orders in the database.
    
    This will:
    1. Query all POs (or limited number if limit specified)
    2. For each PO with items, discover and sync container data from SellerCloud
    3. Store container names, ETA, received dates, and per-item quantities
    
    WARNING: This can make many API calls to SellerCloud (one per PO+SKU combination).
    Use 'limit' parameter to test with a smaller batch first.
    
    Examples:
    - Test with 10 POs: POST /api/v1/purchase-orders/sync-containers?limit=10
    - Sync all POs: POST /api/v1/purchase-orders/sync-containers
    
    Returns:
    - pos_processed: Number of POs that were checked
    - containers_synced: Number of unique containers created/updated
    - links_synced: Number of item-container links created/updated
    """
    from app.services.sync_service import sync_containers_for_all_pos
    
    result = sync_containers_for_all_pos(db, limit=limit)
    
    return {
        "message": f"Container sync completed for {result['pos_processed']} POs",
        "pos_processed": result["pos_processed"],
        "containers_synced": result["containers_synced"],
        "links_synced": result["links_synced"],
    }


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
