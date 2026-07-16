from sqlalchemy.orm import Session

from app.purchase_orders.model import PurchaseOrder
from app.purchase_orders.schema import PurchaseOrderCreate


class PurchaseOrderRepository:

    @staticmethod
    def create(
        db: Session,
        purchase_order: PurchaseOrderCreate,
    ):
        db_purchase_order = PurchaseOrder(
            **purchase_order.model_dump()
        )

        db.add(db_purchase_order)
        db.commit()
        db.refresh(db_purchase_order)

        return db_purchase_order

    @staticmethod
    def get_all(db: Session):
        return (
            db.query(PurchaseOrder)
            .order_by(PurchaseOrder.id.desc())
            .all()
    )

    @staticmethod
    def get_by_id(
        db: Session,
        purchase_order_id: int,
    ):
        return (
            db.query(PurchaseOrder)
            .filter(PurchaseOrder.id == purchase_order_id)
            .first()
    )