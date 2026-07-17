from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session, joinedload

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
    q = db.query(models.PurchaseOrder).options(joinedload(models.PurchaseOrder.items))
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
        .options(joinedload(models.PurchaseOrder.items))
        .filter(models.PurchaseOrder.id == po_id)
        .first()
    )
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


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
