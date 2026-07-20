import uuid
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, EmailStr, ConfigDict, computed_field


# ---------- Auth ----------
class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserCreate(BaseModel):
    """Schema for creating a new user"""
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: str = "user"  # Default role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    full_name: Optional[str] = None
    role: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    message: str


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


# ---------- Vendor ----------
class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_vendor_id: Optional[int] = None
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    is_active: bool
    container_lead_time_days: Optional[int] = None
    updated_at: datetime


class VendorSummary(BaseModel):
    """Vendor summary for nested responses (e.g., in Purchase Orders)"""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_vendor_id: Optional[int] = None
    name: str
    container_lead_time_days: Optional[int] = None


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
class ContainerSummary(BaseModel):
    """Container summary for items"""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_container_id: Optional[int] = None
    container_name: Optional[str] = None
    qty_in_container: int = 0


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
    containers: List[ContainerSummary] = []  # Containers this item is in
    
    @classmethod
    def model_validate(cls, obj, **kwargs):
        """Override to load container information from container_links"""
        instance = super().model_validate(obj, **kwargs)
        
        # Load containers from the link table
        if hasattr(obj, 'container_links') and obj.container_links:
            containers = []
            for link in obj.container_links:
                if link.container:
                    containers.append(ContainerSummary(
                        id=link.container.id,
                        sellercloud_container_id=link.container.sellercloud_container_id,
                        container_name=link.container.container_name,
                        qty_in_container=link.qty_in_container
                    ))
            instance.containers = containers
        
        return instance


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
    vendor: Optional[VendorSummary] = None  # Nested vendor information
    items: List[PurchaseOrderItemOut] = []
    
    # Computed totals for all items
    total_qty_ordered: Optional[int] = None
    total_qty_received: Optional[int] = None
    total_qty_in_container: Optional[int] = None
    
    @classmethod
    def model_validate(cls, obj, **kwargs):
        """Override to compute totals when validating from ORM"""
        instance = super().model_validate(obj, **kwargs)
        
        # Calculate totals from items
        if instance.items:
            instance.total_qty_ordered = sum(item.qty_ordered for item in instance.items)
            instance.total_qty_received = sum(item.qty_received for item in instance.items)
            instance.total_qty_in_container = sum(item.qty_in_container for item in instance.items)
        
        return instance


class PaginatedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    total: int
    page: int
    page_size: int
    results: list
    
    @computed_field
    @property
    def meta(self) -> dict:
        """Computed meta object with pagination info"""
        return {
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "total_pages": (self.total + self.page_size - 1) // self.page_size if self.page_size > 0 else 0,
            "has_next": self.page * self.page_size < self.total,
            "has_prev": self.page > 1
        }


class SyncResponse(BaseModel):
    entity_type: str
    status: str
    records_synced: int
    message: Optional[str] = None
