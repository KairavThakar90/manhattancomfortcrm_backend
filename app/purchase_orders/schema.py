from pydantic import BaseModel
from datetime import datetime


class PurchaseOrderCreate(BaseModel):
    po_number: str
    vendor_name: str
    country: str
    status: str
    ordered_qty: int
    received_qty: int = 0


class PurchaseOrderResponse(BaseModel):
    id: int
    po_number: str
    vendor_name: str
    country: str
    status: str
    ordered_qty: int
    received_qty: int
    created_at: datetime

    class Config:
        from_attributes = True