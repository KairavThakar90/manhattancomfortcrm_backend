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
    estimated_arrival_date: Optional[datetime] = None  # Container ETA
    received_date: Optional[datetime] = None  # Actual received date
    qty_in_container: int = 0


class PurchaseOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_ordered: int
    qty_received: int
    qty_remaining: Optional[int] = None  # Calculated: qty_ordered - qty_received
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
        
        # Calculate remaining quantity
        instance.qty_remaining = instance.qty_ordered - instance.qty_received
        
        # Load containers from the link table
        if hasattr(obj, 'container_links') and obj.container_links:
            containers = []
            for link in obj.container_links:
                if link.container:
                    containers.append(ContainerSummary(
                        id=link.container.id,
                        sellercloud_container_id=link.container.sellercloud_container_id,
                        container_name=link.container.container_name,
                        estimated_arrival_date=link.container.estimated_arrival_date,
                        received_date=link.container.received_date,
                        qty_in_container=link.qty_in_container
                    ))
            instance.containers = containers
        
        return instance


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_po_id: Optional[int] = None
    purchase_title: Optional[str] = None
    order_number: Optional[str] = None  # Extracted from purchase_title (number after #)
    purchase_order_status_code: Optional[int] = None
    receiving_status_code: Optional[int] = None
    status_label: Optional[str] = None
    created_on: Optional[datetime] = None
    date_ordered: Optional[datetime] = None
    expected_delivery_date: Optional[datetime] = None
    invoice_date: Optional[datetime] = None
    container_lead_time_days: Optional[int] = None  # PO-level lead time
    total_amount: Optional[float] = None
    currency: str
    company_id: Optional[uuid.UUID] = None
    vendor_id: Optional[uuid.UUID] = None
    vendor: Optional[VendorSummary] = None  # Nested vendor information
    items: List[PurchaseOrderItemOut] = []
    
    # Computed totals for all items
    total_item_count: Optional[int] = None  # Count of items in this PO
    total_qty_ordered: Optional[int] = None
    total_qty_received: Optional[int] = None
    total_qty_remaining: Optional[int] = None  # Calculated: total_qty_ordered - total_qty_received
    total_qty_in_container: Optional[int] = None
    
    # Container information
    container_names: List[str] = []  # All unique container names for this PO
    
    # Status flags
    is_invoice_delayed: Optional[str] = None  # "Yes" or "No"
    is_container_overdue: Optional[str] = None  # "Yes" or "No"
    
    @classmethod
    def model_validate(cls, obj, **kwargs):
        """Override to compute totals and status flags when validating from ORM"""
        from datetime import datetime, timedelta, timezone
        import re
        
        # First, manually validate items to ensure containers are loaded
        validated_items = []
        if hasattr(obj, 'items') and obj.items:
            for item in obj.items:
                validated_items.append(PurchaseOrderItemOut.model_validate(item))
        
        # Now validate the PO, but replace items with our pre-validated ones
        instance = super().model_validate(obj, **kwargs)
        instance.items = validated_items
        
        # Extract order number from purchase_title (e.g., "Created for Order# 6962293" -> "6962293")
        if instance.purchase_title:
            match = re.search(r'#\s*(\d+)', instance.purchase_title)
            if match:
                instance.order_number = match.group(1)
        
        # Calculate totals from items
        if instance.items:
            instance.total_item_count = len(instance.items)
            instance.total_qty_ordered = sum(item.qty_ordered for item in instance.items)
            instance.total_qty_received = sum(item.qty_received for item in instance.items)
            instance.total_qty_remaining = sum(item.qty_remaining for item in instance.items if item.qty_remaining)
            instance.total_qty_in_container = sum(item.qty_in_container for item in instance.items)
            
            # Collect unique container names from all items
            container_names_set = set()
            for item in instance.items:
                for container in item.containers:
                    if container.container_name:
                        container_names_set.add(container.container_name)
            instance.container_names = sorted(list(container_names_set))
        else:
            instance.total_item_count = 0
        
        # Calculate status flags
        today = datetime.now(timezone.utc).date()
        
        # 1. Check if invoice is delayed (missing after 10 days)
        if instance.invoice_date:
            instance.is_invoice_delayed = "No"  # Has invoice
        elif instance.created_on:
            days_since_creation = (today - instance.created_on.date()).days
            instance.is_invoice_delayed = "Yes" if days_since_creation > 10 else "No"
        else:
            instance.is_invoice_delayed = "No"
        
        # 2. Check if container is overdue based on PO lead time (not vendor lead time)
        if instance.invoice_date and instance.container_lead_time_days:
            expected_arrival = instance.invoice_date.date() + timedelta(days=instance.container_lead_time_days)
            instance.is_container_overdue = "Yes" if expected_arrival < today else "No"
        else:
            instance.is_container_overdue = "No"  # No invoice or no lead time set
        
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
