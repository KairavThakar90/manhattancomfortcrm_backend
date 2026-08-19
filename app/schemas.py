import uuid
from datetime import datetime
from typing import Optional, List, Union, Dict, Any

from pydantic import BaseModel, EmailStr, ConfigDict, computed_field, Field, AliasChoices


# ---------- Auth ----------
class UserLogin(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginRequest(BaseModel):
    token: str


class UserCreate(BaseModel):
    """Schema for creating a new user"""
    first_name: str
    last_name: str
    email: EmailStr
    password: str
    role: str = "user"  # Default role
    vendor_id: Optional[uuid.UUID] = None
    warehouse_id: Optional[uuid.UUID] = None
    # New fields for vendor registration:
    vendor_name: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    payment_terms: Optional[str] = None
    lead_time: Optional[int] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    role: str
    vendor_id: Optional[uuid.UUID] = None
    vendor: Optional['VendorSummary'] = None
    warehouse_id: Optional[uuid.UUID] = None
    warehouse: Optional['WarehouseOut'] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    payment_terms: Optional[str] = None
    container_lead_time_days: Optional[int] = None
    
    # Notification Preferences
    notify_new_user: bool = False
    notify_trucker_email: bool = False
    notify_invoice_delayed: bool = False
    notify_shipment_delayed: bool = False
    
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None


class UserMentionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: EmailStr
    full_name: Optional[str] = None
    role: str


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    vendor_id: Optional[uuid.UUID] = None
    warehouse_id: Optional[uuid.UUID] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    payment_terms: Optional[str] = None
    container_lead_time_days: Optional[int] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    notify_new_user: Optional[bool] = None
    notify_trucker_email: Optional[bool] = None
    notify_invoice_delayed: Optional[bool] = None
    notify_shipment_delayed: Optional[bool] = None


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    message: str


class UpdatePasswordRequest(BaseModel):
    password: str
    confirm_password: str

class Login2FAResponse(BaseModel):
    message: str
    requires_2fa: bool
    email: str

class Verify2FARequest(BaseModel):
    email: EmailStr
    code: str


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


class CompanySummary(BaseModel):
    """Company summary for nested responses"""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_company_id: Optional[int] = None
    name: str


# ---------- Channels ----------
class ChannelSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str

class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
    payment_terms: Optional[str] = None
    is_active: bool
    container_lead_time_days: Optional[int] = None
    updated_at: datetime
    po_count: int = 0


class VendorSummary(BaseModel):
    """Vendor summary for nested responses (e.g., in Purchase Orders)"""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_vendor_id: Optional[int] = None
    name: str
    container_lead_time_days: Optional[int] = None


class VendorUpdate(BaseModel):
    name: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    payment_terms: Optional[str] = None
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
    po_count: int = 0


# ---------- Purchase Order ----------
class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_warehouse_id: Optional[int] = None
    name: Optional[str] = None
    is_default: Optional[bool] = None
    warehouse_type: Optional[str] = None
    is_sellable: Optional[bool] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class POItemQuantityUpdate(BaseModel):
    qty_ordered: int = Field(gt=0, description="New ordered quantity")


class POItemBasicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_item_id: Optional[int] = None
    sku: Optional[str] = None
    qty_ordered: int


class ContainerSummary(BaseModel):
    """Container summary for items — includes qty_in_container from the join table."""
    model_config = ConfigDict(from_attributes=True)
    id: Optional[uuid.UUID] = None
    sellercloud_container_id: Optional[int] = None
    container_name: Optional[str] = None
    estimated_arrival_date: Optional[datetime] = None
    received_date: Optional[datetime] = None
    date_emptied: Optional[datetime] = None
    qty_in_container: Optional[int] = None

class ContainerAttachmentOut(BaseModel):
    id: uuid.UUID
    file_name: str
    file_url: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class ContainerOut(BaseModel):
    """Container list item — includes summary counts and received status."""
    id: uuid.UUID
    sellercloud_container_id: Optional[int] = None
    container_name: Optional[str] = None
    estimated_arrival_date: Optional[datetime] = None
    received_date: Optional[datetime] = None
    is_received: bool = False           # True when received_date is not None
    warehouse_id: Optional[uuid.UUID] = None
    warehouse: Optional[WarehouseOut] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    # Lifecycle Fields
    date_dropped_off: Optional[datetime] = None
    door: Optional[str] = None
    trucker_email: Optional[str] = None
    date_emptied: Optional[datetime] = None
    unloaded_by: Optional[str] = None
    unload_cost: Optional[float] = None
    container_cost_drayage: Optional[float] = None
    customs_duty_misc: Optional[float] = None
    per_diem: Optional[float] = None
    country_of_origin: Optional[str] = None
    receiving_closure_notes: Optional[str] = None
    factory_credit_needed: Optional[str] = None
    
    # Summary counts (populated by the list endpoint, not from ORM directly)
    total_items: Optional[int] = None
    total_qty_in_container: Optional[int] = None
    total_qty_received: Optional[int] = None
    unique_pos: Optional[int] = None
    attachments: List[ContainerAttachmentOut] = []

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    def sellercloud_link(self) -> Optional[str]:
        if self.sellercloud_container_id:
            return f"https://cd.cwa.sellercloud.com/Purchasing/ShippingContainer.aspx?ContainerID={self.sellercloud_container_id}"
        return None


class ContainerListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    meta: dict = {}
    results: list[ContainerOut] = []

class ContainerExportRequest(BaseModel):
    container_ids: Optional[list[str]] = Field(default=None, description="List of container UUIDs to export")
    is_received: Optional[bool] = Field(default=None, description="Filter: true = received, false = pending")
    columns: Optional[list[str]] = Field(default=None, description="List of columns to export")

class ContainerDetailItemOut(BaseModel):
    """One line item inside a container detail response."""
    po_item_id: uuid.UUID
    sellercloud_item_id: Optional[int] = None
    sellercloud_po_id: Optional[int] = None
    po_title: Optional[str] = None
    vendor_name: Optional[str] = None
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_in_container: int
    qty_ordered: int
    qty_received: int
    qty_remaining: int
    is_fully_received: bool = False     # True when qty_received >= qty_ordered
    unit_price: Optional[float] = None


class ContainerDetailOut(BaseModel):
    """Full container detail with all items and summary."""
    id: uuid.UUID
    sellercloud_container_id: Optional[int] = None
    container_name: Optional[str] = None
    estimated_arrival_date: Optional[datetime] = None
    received_date: Optional[datetime] = None
    is_received: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    warehouse: Optional[WarehouseOut] = None

    # Lifecycle Fields
    date_dropped_off: Optional[datetime] = None
    door: Optional[str] = None
    trucker_email: Optional[str] = None
    date_emptied: Optional[datetime] = None
    unloaded_by: Optional[str] = None
    unload_cost: Optional[float] = None
    container_cost_drayage: Optional[float] = None
    customs_duty_misc: Optional[float] = None
    per_diem: Optional[float] = None
    country_of_origin: Optional[str] = None
    receiving_closure_notes: Optional[str] = None
    factory_credit_needed: Optional[str] = None

    summary: dict = {}
    items: List[ContainerDetailItemOut] = []
    attachments: List[ContainerAttachmentOut] = []

    @computed_field
    def sellercloud_link(self) -> Optional[str]:
        if self.sellercloud_container_id:
            return f"https://cd.cwa.sellercloud.com/Purchasing/ShippingContainer.aspx?ContainerID={self.sellercloud_container_id}"
        return None


class ContainerItemCreate(BaseModel):
    po_item_id: Optional[uuid.UUID] = Field(default=None, description="Local UUID of PO item (optional if sellercloud_item_id or sellercloud_po_id+sku is provided)")
    sellercloud_po_id: Optional[int] = Field(default=None, description="SellerCloud PO ID")
    sellercloud_item_id: Optional[int] = Field(default=None, description="SellerCloud PO Item ID")
    sku: Optional[str] = Field(default=None, description="Product SKU")
    qty_in_container: int = Field(gt=0, description="Quantity of this item in the container")


class ContainerCreate(BaseModel):
    container_name: str = Field(min_length=1, max_length=255, description="Container name/number")
    estimated_arrival_date: Optional[datetime] = Field(default=None, description="Expected arrival date")
    received_date: Optional[datetime] = Field(default=None, description="Actual received date")
    warehouse_id: Optional[Union[uuid.UUID, int]] = Field(default=None, description="Warehouse UUID or SellerCloud integer ID")
    
    # Lifecycle Fields
    date_dropped_off: Optional[datetime] = None
    door: Optional[str] = None
    date_emptied: Optional[datetime] = None
    unloaded_by: Optional[str] = None
    unload_cost: Optional[float] = None
    container_cost_drayage: Optional[float] = None
    customs_duty_misc: Optional[float] = None
    per_diem: Optional[float] = None
    country_of_origin: Optional[str] = None
    receiving_closure_notes: Optional[str] = None
    factory_credit_needed: Optional[str] = None
    
    items: List[ContainerItemCreate] = Field(min_length=1, description="List of items in this container")


class ContainerUpdate(BaseModel):
    container_name: Optional[str] = Field(default=None, min_length=1, max_length=255, description="Updated container name/number")
    estimated_arrival_date: Optional[datetime] = Field(default=None, description="Updated expected arrival date")
    received_date: Optional[datetime] = Field(default=None, description="Updated actual received date")
    
    # Lifecycle Fields
    date_dropped_off: Optional[datetime] = None
    door: Optional[str] = None
    date_emptied: Optional[datetime] = None
    unloaded_by: Optional[str] = None
    unload_cost: Optional[float] = None
    container_cost_drayage: Optional[float] = None
    customs_duty_misc: Optional[float] = None
    per_diem: Optional[float] = None
    country_of_origin: Optional[str] = None
    receiving_closure_notes: Optional[str] = None
    factory_credit_needed: Optional[str] = None
    trucker_email: Optional[str] = None


class ContainerAddItems(BaseModel):
    items: List[ContainerItemCreate] = Field(min_length=1, description="List of items to add to this container")


# ---------- PO items for container creation ----------
class POItemForContainerOut(BaseModel):
    """Shape of one PO line item when building a new container."""
    po_item_id: uuid.UUID
    sellercloud_item_id: Optional[int] = None
    sellercloud_po_id: Optional[int] = None
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_ordered: int
    qty_received: int
    qty_remaining: int                  # qty_ordered - qty_received
    qty_already_in_containers: int      # running total across all existing containers
    qty_available_for_container: int    # qty_ordered - qty_already_in_containers (can be 0)
    existing_containers: List[ContainerSummary] = []


class POItemsForContainerResponse(BaseModel):
    """Response for GET /containers/po-items/{sellercloud_po_id}"""
    po_id: uuid.UUID
    sellercloud_po_id: int
    po_title: Optional[str] = None
    vendor_name: Optional[str] = None
    items: List[POItemForContainerOut] = []
    summary: dict = {}



class ContainerItemOut(BaseModel):
    po_id: uuid.UUID
    po_sellercloud_id: Optional[int] = None
    po_title: Optional[str] = None
    vendor_name: Optional[str] = None
    item_id: uuid.UUID
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_in_container: int
    qty_ordered: int
    qty_received: int
    qty_remaining: int
    unit_price: Optional[float] = None


class PurchaseOrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_item_id: Optional[int] = None
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
    comments: List['POItemCommentOut'] = []  # Added item comments
    comments_count: int = 0                  # Computed comment count
    
    @classmethod
    def model_validate(cls, obj, **kwargs):
        """Override to load container information from container_links"""
        instance = super().model_validate(obj, **kwargs)
        
        # Calculate remaining quantity
        qty_ord = instance.qty_ordered or 0
        qty_rec = instance.qty_received or 0
        qty_in_cont = instance.qty_in_container or 0
        instance.qty_remaining = max(0, qty_ord - max(qty_rec, qty_in_cont))
        
        # Calculate comment count
        instance.comments_count = len(instance.comments) if hasattr(instance, 'comments') and instance.comments else 0
        
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
                        date_emptied=link.container.date_emptied,
                        qty_in_container=link.qty_in_container
                    ))
            instance.containers = containers
        
        return instance


class POCommentCreate(BaseModel):
    comment: str = Field(
        min_length=1,
        validation_alias=AliasChoices("comment", "text", "message", "content"),
    )
    parent_id: Optional[uuid.UUID] = None
    tagged_user_ids: List[uuid.UUID] = []
    model_config = ConfigDict(populate_by_name=True)


class POCommentUpdate(BaseModel):
    comment: str
    tagged_user_ids: List[uuid.UUID] = []


class AttachmentOut(BaseModel):
    id: uuid.UUID
    file_name: str
    file_url: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class POCommentOut(BaseModel):
    id: uuid.UUID
    comment: str
    created_at: datetime
    user_id: Optional[uuid.UUID] = None
    user_name: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None
    is_edited: bool = False
    attachments: List[AttachmentOut] = []
    
    model_config = ConfigDict(from_attributes=True)

class POItemCommentCreate(BaseModel):
    comment: str
    parent_id: Optional[uuid.UUID] = None
    tagged_user_ids: List[uuid.UUID] = []

class POItemCommentOut(BaseModel):
    id: uuid.UUID
    comment: str
    created_at: datetime
    user_id: Optional[uuid.UUID] = None
    user_name: Optional[str] = None
    parent_id: Optional[uuid.UUID] = None
    is_edited: bool = False
    attachments: List[AttachmentOut] = []
    
    model_config = ConfigDict(from_attributes=True)

class POContainerDetailOut(BaseModel):
    id: uuid.UUID
    sellercloud_container_id: Optional[int] = None
    container_name: Optional[str] = None
    estimated_arrival_date: Optional[datetime] = None
    received_date: Optional[datetime] = None
    date_emptied: Optional[datetime] = None

class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_warehouse_id: Optional[int] = None
    name: Optional[str] = None
    is_default: Optional[bool] = None
    warehouse_type: Optional[str] = None
    is_sellable: Optional[bool] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sellercloud_po_id: Optional[int] = None
    purchase_title: Optional[str] = None
    order_number: Optional[str] = None  # Extracted from purchase_title (number after #)
    channel_order_id: Optional[str] = None
    channel_id: Optional[uuid.UUID] = None
    channel: Optional[ChannelSummary] = None
    warehouse_id: Optional[uuid.UUID] = None
    warehouse: Optional[WarehouseOut] = None
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
    company: Optional[CompanySummary] = None
    vendor_id: Optional[uuid.UUID] = None
    vendor: Optional[VendorSummary] = None  # Nested vendor information
    customer_id: Optional[uuid.UUID] = None
    customer: Optional['CustomerOut'] = None
    status: Optional[str] = None
    items: List[PurchaseOrderItemOut] = []
    comments: List[POCommentOut] = []
    
    @computed_field
    def sellercloud_link(self) -> Optional[str]:
        if hasattr(self, 'order_number') and self.order_number:
            return f"https://cd.cwa.sellercloud.com/Orders/Orders_Details.aspx?ID={self.order_number}"
        return None

    @computed_field
    def delta_sellercloud_link(self) -> Optional[str]:
        if self.sellercloud_po_id:
            return f"https://cd.delta.sellercloud.com/purchasing/po-details.aspx?id={self.sellercloud_po_id}"
        return None
    # Computed totals for all items
    total_item_count: Optional[int] = None  # Count of items in this PO
    total_qty_ordered: Optional[int] = None
    total_qty_received: Optional[int] = None
    total_qty_remaining: Optional[int] = None  # Calculated: total_qty_ordered - total_qty_received
    total_qty_in_container: Optional[int] = None
    total_comments_count: int = 0
    delay_reason: Optional[str] = None
    
    # Nested Information
    container_names: List[str] = []  # All unique container names for this PO
    container_details: List[POContainerDetailOut] = []  # Detailed container info
    
    # Status flags
    is_invoice_delayed: Optional[str] = None  # "Yes" or "No"
    is_container_overdue: Optional[str] = None  # "Yes" or "No"
    
    # Delay Details (Per Container)
    delay_details: Optional[Dict[str, Any]] = Field(default_factory=dict)
    
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
        
        # Sort items based on the latest comment date (descending), items with no comments go last
        def get_latest_comment_date(item):
            if hasattr(item, 'comments') and item.comments:
                dates = [c.created_at for c in item.comments if c.created_at]
                if dates:
                    return max(dates)
            return datetime.min.replace(tzinfo=timezone.utc)
            
        validated_items.sort(key=get_latest_comment_date, reverse=True)
        instance.items = validated_items
        
        # Extract order number from purchase_title (e.g., "Created for Order# 6962293" -> "6962293")
        if instance.purchase_title:
            if "cloned from po" in instance.purchase_title.lower():
                instance.order_number = "Stock"
            else:
                match = re.search(r'#\s*(\d+)', instance.purchase_title)
                if match:
                    instance.order_number = match.group(1)
        
        # Calculate totals from items
        if instance.items:
            instance.total_item_count = len(instance.items)
            instance.total_qty_ordered = sum((item.qty_ordered or 0) for item in instance.items)
            instance.total_qty_received = sum((item.qty_received or 0) for item in instance.items)
            instance.total_qty_remaining = sum((item.qty_remaining or 0) for item in instance.items if item.qty_remaining)
            instance.total_qty_in_container = sum((item.qty_in_container or 0) for item in instance.items)
            
            # Collect unique container names from all items
            container_names_set = set()
            container_details_map = {}
            for item in instance.items:
                for container in item.containers:
                    if container.container_name:
                        container_names_set.add(container.container_name)
                        if str(container.id) not in container_details_map:
                            container_details_map[str(container.id)] = {
                                "id": container.id,
                                "sellercloud_container_id": container.sellercloud_container_id,
                                "container_name": container.container_name,
                                "estimated_arrival_date": container.estimated_arrival_date,
                                "received_date": container.received_date,
                                "date_emptied": container.date_emptied
                            }
            instance.container_names = sorted(list(container_names_set))
            instance.container_details = [POContainerDetailOut(**detail) for detail in container_details_map.values()]
        else:
            instance.total_item_count = 0
        
        # Calculate status flags
        today = datetime.now(timezone.utc).date()
        
        # Calculate total comments
        total_comments = len(instance.comments) if hasattr(instance, 'comments') and instance.comments else 0
        if instance.items:
            for item in instance.items:
                if hasattr(item, 'comments') and item.comments:
                    total_comments += len(item.comments)
        instance.total_comments_count = total_comments
        
        # 1. Check if invoice is delayed (missing after 10 days)
        if instance.invoice_date:
            instance.is_invoice_delayed = "No"  # Has invoice
        elif instance.created_on:
            days_since_creation = (today - instance.created_on.date()).days
            instance.is_invoice_delayed = "Yes" if days_since_creation > 10 else "No"
        else:
            instance.is_invoice_delayed = "No"
        
        # 2. Check container delays individually
        instance.delay_details = {
            "arrived_containers": [],
            "delayed_containers": [],
            "unassigned_delayed_items": False
        }
        
        if instance.invoice_date:
            po_lead_time = instance.container_lead_time_days
            if not po_lead_time and hasattr(instance, 'vendor') and instance.vendor:
                po_lead_time = instance.vendor.container_lead_time_days
            
            po_expected_arrival = None
            if po_lead_time:
                po_expected_arrival = instance.invoice_date.date() + timedelta(days=po_lead_time)
                
            has_delayed_containers = False
            
            # Check containers
            if hasattr(instance, 'container_details') and instance.container_details:
                for container in instance.container_details:
                    # Is it arrived?
                    if container.received_date or container.date_emptied:
                        instance.delay_details["arrived_containers"].append(container.container_name or str(container.sellercloud_container_id))
                        continue
                        
                    # Not arrived. Is it delayed?
                    container_delayed = False
                    if container.estimated_arrival_date:
                        if container.estimated_arrival_date.date() < today:
                            container_delayed = True
                    elif po_expected_arrival:
                        if po_expected_arrival < today:
                            container_delayed = True
                            
                    if container_delayed:
                        instance.delay_details["delayed_containers"].append(container.container_name or str(container.sellercloud_container_id))
                        has_delayed_containers = True
                        
                # Check for unassigned items if PO lead time has passed
                if po_expected_arrival and po_expected_arrival < today:
                    items_in_containers = sum(1 for item in instance.items if hasattr(item, 'container_names') and item.container_names)
                    if items_in_containers < len(instance.items):
                        instance.delay_details["unassigned_delayed_items"] = True
                        has_delayed_containers = True
                        
            else:
                # No containers attached. Are we past PO lead time?
                if po_expected_arrival and po_expected_arrival < today:
                    instance.delay_details["unassigned_delayed_items"] = True
                    has_delayed_containers = True

            instance.is_container_overdue = "Yes" if has_delayed_containers else "No"
        else:
            instance.is_container_overdue = "No"  # No invoice
            
        # 3. Dynamic status calculation based on received quantities
        qty_ord = instance.total_qty_ordered or 0
        qty_rec = instance.total_qty_received or 0
        if qty_ord > 0:
            if qty_rec >= qty_ord:
                instance.status = "SHIPPED"
            elif qty_rec > 0:
                instance.status = "PARTIALLY_SHIPPED"
        
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
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None
    errors: Optional[list[str]] = None
    data: Optional[dict] = None
    
    # Legacy fields
    entity_type: Optional[str] = None
    status: Optional[str] = None
    records_synced: Optional[int] = 0


class POExportRequest(BaseModel):
    po_ids: Optional[List[Union[int, uuid.UUID]]] = Field(default=None, description="SellerCloud PO IDs (int) or internal PO UUIDs. Omit to export all purchase orders.")
    filter_status: Optional[str] = Field(default=None, description="invoice_delayed, delivery_delayed, or lefts_items")
    columns: Optional[List[str]] = Field(default=None, description="Subset/order of column names to include. Omit to include all columns.")

class ValidateContainerRowRequest(BaseModel):
    po_id: str
    sku: str
    qty: Optional[int] = 0

class ValidateContainerBulkRequest(BaseModel):
    items: List[ValidateContainerRowRequest]

class UserActivityLogCreate(BaseModel):
    action: str = Field(..., description="Action name, e.g., VIEW_PO, CLICK_BUTTON")
    category: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

class UserActivityLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    action: str
    category: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    created_at: datetime
    
    # Optional nested user details for display
    user: Optional[UserOut] = None
    user_name: Optional[str] = None
    human_readable_message: Optional[str] = None

class POStatusUpdate(BaseModel):
    status: Optional[str] = Field(None, description="E.g. NOT_STARTED, IN_PRODUCTION, DELAYED, COMPLETED, NOT_PLANNED, PLANNED, PARTIALLY_SHIPPED, SHIPPED")
    delay_reason: Optional[str] = None
