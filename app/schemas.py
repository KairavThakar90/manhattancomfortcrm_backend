import uuid
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, EmailStr, ConfigDict


# ---------- Auth ----------
class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    full_name: Optional[str] = None
    role: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- Company ----------
class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_company_id: Optional[int] = None
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    is_active: bool
    updated_at: datetime


# ---------- Customer ----------
class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_customer_id: Optional[int] = None
    company_id: Optional[uuid.UUID] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    billing_city: Optional[str] = None
    shipping_city: Optional[str] = None
    updated_at: datetime


# ---------- Purchase Order ----------
class PurchaseOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_ordered: int
    qty_received: int
    qty_in_container: int
    unit_price: float
    qty_cases_ordered: int
    qty_units_per_case: int
    case_price: float
    is_bundle_component: bool
    expected_delivery_date: Optional[datetime] = None


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_po_id: Optional[int] = None
    purchase_title: Optional[str] = None
    purchase_order_status_code: Optional[int] = None
    receiving_status_code: Optional[int] = None
    status_label: Optional[str] = None
    created_on: Optional[datetime] = None
    date_ordered: Optional[datetime] = None
    expected_delivery_date: Optional[datetime] = None
    invoice_date: Optional[datetime] = None
    total_amount: Optional[float] = None
    currency: str
    company_id: Optional[uuid.UUID] = None
    vendor_id: Optional[uuid.UUID] = None
    items: List[PurchaseOrderItemOut] = []


class PaginatedResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: list


class SyncResponse(BaseModel):
    entity_type: str
    status: str
    records_synced: int
    message: Optional[str] = None
