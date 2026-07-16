from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from app.core.database import Base


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)

    po_number = Column(String(100), unique=True, nullable=False)

    vendor_name = Column(String(255), nullable=False)

    country = Column(String(100), nullable=False)

    status = Column(String(100), nullable=False)

    ordered_qty = Column(Integer, nullable=False)

    received_qty = Column(Integer, default=0)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )