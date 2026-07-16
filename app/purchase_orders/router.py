from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.purchase_orders.schema import (
    PurchaseOrderCreate,
    PurchaseOrderResponse,
)
from app.purchase_orders.service import PurchaseOrderService
from typing import List
router = APIRouter(
    prefix="/api/v1/purchase-orders",
    tags=["Purchase Orders"],
)


@router.post(
    "",
    response_model=PurchaseOrderResponse,
)
def create_purchase_order(
    purchase_order: PurchaseOrderCreate,
    db: Session = Depends(get_db),
):
    return PurchaseOrderService.create(
        db,
        purchase_order,
    )

@router.get(
    "",
    response_model=List[PurchaseOrderResponse],
)
def get_purchase_orders(
    db: Session = Depends(get_db),
):
    return PurchaseOrderService.get_all(db)

@router.get(
    "/{purchase_order_id}",
    response_model=PurchaseOrderResponse,
)
def get_purchase_order(
    purchase_order_id: int,
    db: Session = Depends(get_db),
):
    return PurchaseOrderService.get_by_id(
        db,
        purchase_order_id,
    )