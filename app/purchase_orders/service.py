from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.purchase_orders.repository import PurchaseOrderRepository
from app.purchase_orders.schema import PurchaseOrderCreate


class PurchaseOrderService:

    @staticmethod
    def create(
        db: Session,
        purchase_order: PurchaseOrderCreate,
    ):
        return PurchaseOrderRepository.create(
            db,
            purchase_order,
        )

    @staticmethod
    def get_all(db: Session):
        return PurchaseOrderRepository.get_all(db)


    @staticmethod
    def get_by_id(
        db: Session,
        purchase_order_id: int,
    ):
        purchase_order = PurchaseOrderRepository.get_by_id(
            db,
            purchase_order_id,
        )

        if not purchase_order:
            raise HTTPException(
                status_code=404,
                detail="Purchase Order not found",
            )

        return purchase_order