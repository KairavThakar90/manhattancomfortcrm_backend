import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Numeric, Integer, Text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship, deferred

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    first_name = Column(String(120))
    last_name = Column(String(120))
    full_name = Column(String(255))
    role = Column(String(50), default="user", nullable=False)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True)
    warehouse_id = Column(UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True)
    
    # Registration info (mirrored from Vendor for clarity on per-user level)
    country = Column(String(120), nullable=True)
    phone = Column(String(50), nullable=True)
    payment_terms = Column(String(255), nullable=True)
    container_lead_time_days = Column(Integer, nullable=True)
    # Notification Preferences
    notify_new_user = Column(Boolean, default=False)
    notify_trucker_email = Column(Boolean, default=False)
    notify_invoice_delayed = Column(Boolean, default=False)
    notify_shipment_delayed = Column(Boolean, default=False)
    
    is_active = Column(Boolean, default=True, nullable=False)
    otp_code = Column(String(10), nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)
    
    vendor = relationship("Vendor")
    warehouse = relationship("Warehouse")
    po_comments = relationship("PurchaseOrderComment", back_populates="user")
    po_item_comments = relationship("PurchaseOrderItemComment", back_populates="user")


class Company(Base):
    __tablename__ = "companies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_company_id = Column(Integer, unique=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255))
    phone = Column(String(50))
    address_line1 = Column(String(255))
    address_line2 = Column(String(255))
    city = Column(String(120))
    state = Column(String(120))
    postal_code = Column(String(20))
    country = Column(String(120))
    is_active = Column(Boolean, default=True)
    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    customers = relationship("Customer", back_populates="company")
    purchase_orders = relationship("PurchaseOrder", back_populates="company")


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_vendor_id = Column(Integer, unique=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255))
    phone = Column(String(50))
    address_line1 = Column(String(255))
    city = Column(String(120))
    state = Column(String(120))
    postal_code = Column(String(20))
    country = Column(String(120))
    payment_terms = Column(String(255))
    is_active = Column(Boolean, default=True)
    container_lead_time_days = Column(Integer)  # days from payment/order to first container arrival, set per vendor
    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    purchase_orders = relationship("PurchaseOrder", back_populates="vendor")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_customer_id = Column(Integer, unique=True, index=True)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"))
    first_name = Column(String(120))
    last_name = Column(String(120))
    email = Column(String(255))
    phone = Column(String(50))
    billing_address_line1 = Column(String(255))
    billing_city = Column(String(120))
    billing_state = Column(String(120))
    billing_postal_code = Column(String(20))
    billing_country = Column(String(120))
    shipping_address_line1 = Column(String(255))
    shipping_city = Column(String(120))
    shipping_state = Column(String(120))
    shipping_postal_code = Column(String(20))
    shipping_country = Column(String(120))
    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="customers")


class Warehouse(Base):
    __tablename__ = "warehouses"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_warehouse_id = Column(Integer, unique=True, index=True)
    name = Column(String(255))
    is_default = Column(Boolean, default=False)
    warehouse_type = Column(String(50))
    is_sellable = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    
    purchase_orders = relationship("PurchaseOrder", back_populates="warehouse")
    containers = relationship("ShippingContainer", back_populates="warehouse")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_po_id = Column(Integer, unique=True, index=True)
    purchase_title = Column(String(255))
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"))
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"))
    purchase_order_status_code = Column(Integer)
    receiving_status_code = Column(Integer)
    status_label = Column(String(50), index=True)
    created_on = Column(DateTime(timezone=True))
    date_ordered = Column(DateTime(timezone=True))
    expected_delivery_date = Column(DateTime(timezone=True))
    invoice_date = Column(DateTime(timezone=True))
    container_lead_time_days = Column(Integer)  # Days from invoice date to container arrival, set per PO
    total_amount = Column(Numeric(14, 2))
    currency = Column(String(10), default="USD")
    notes = Column(Text)
    status = Column(String(50), nullable=True)
    
    # Delay Tracking
    delay_reason = Column(Text, nullable=True)
    delay_notification_sent = Column(Boolean, default=False)
    
    # Warehouse Info (ForeignKey to Warehouse table)
    warehouse_id = Column(UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="SET NULL"))
    
    # Customer Info (ForeignKey to Customer table)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    
    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="purchase_orders")
    vendor = relationship("Vendor", back_populates="purchase_orders")
    warehouse = relationship("Warehouse", back_populates="purchase_orders")
    customer = relationship("Customer", backref="purchase_orders")
    items = relationship("PurchaseOrderItem", back_populates="purchase_order", cascade="all, delete-orphan")
    comments = relationship("PurchaseOrderComment", back_populates="purchase_order", cascade="all, delete-orphan")


class PurchaseOrderComment(Base):
    __tablename__ = "purchase_order_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_id = Column(UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_comments.id", ondelete="CASCADE"), nullable=True)
    comment = Column(Text, nullable=False)
    is_edited = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    purchase_order = relationship("PurchaseOrder", back_populates="comments")
    user = relationship("User", back_populates="po_comments")
    replies = relationship("PurchaseOrderComment")
    attachments = relationship("PurchaseOrderCommentAttachment", back_populates="comment", cascade="all, delete-orphan")

class PurchaseOrderCommentAttachment(Base):
    __tablename__ = "purchase_order_comment_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    comment_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_comments.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_url = Column(Text, nullable=False)
    content_type = Column(String(100), nullable=True)
    size = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    comment = relationship("PurchaseOrderComment", back_populates="attachments")


class PurchaseOrderItemComment(Base):
    __tablename__ = "purchase_order_item_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_item_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_items.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_item_comments.id", ondelete="CASCADE"), nullable=True)
    comment = Column(Text, nullable=False)
    is_edited = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    item = relationship("PurchaseOrderItem", back_populates="comments")
    user = relationship("User", back_populates="po_item_comments")
    replies = relationship("PurchaseOrderItemComment")
    attachments = relationship("PurchaseOrderItemCommentAttachment", back_populates="comment", cascade="all, delete-orphan")

class PurchaseOrderItemCommentAttachment(Base):
    __tablename__ = "purchase_order_item_comment_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    comment_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_item_comments.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_url = Column(Text, nullable=False)
    content_type = Column(String(100), nullable=True)
    size = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    comment = relationship("PurchaseOrderItemComment", back_populates="attachments")



class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_id = Column(UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False)
    sellercloud_item_id = Column(Integer)
    sku = Column(String(120))
    product_name = Column(String(255))
    qty_ordered = Column(Integer, default=0)
    qty_received = Column(Integer, default=0)
    qty_in_container = Column(Integer, default=0)
    unit_price = Column(Numeric(14, 2), default=0)
    qty_cases_ordered = Column(Integer, default=0)
    qty_units_per_case = Column(Integer, default=0)
    case_price = Column(Numeric(14, 2), default=0)
    is_bundle_component = Column(Boolean, default=False)
    parent_sellercloud_item_id = Column(Integer)
    expected_delivery_date = Column(DateTime(timezone=True))
    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    purchase_order = relationship("PurchaseOrder", back_populates="items")
    container_links = relationship("PurchaseOrderItemContainer", back_populates="item", cascade="all, delete-orphan")
    comments = relationship("PurchaseOrderItemComment", back_populates="item", cascade="all, delete-orphan")


class ShippingContainer(Base):
    __tablename__ = "shipping_containers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_container_id = Column(Integer, unique=True, index=True)
    container_name = Column(String(255))
    estimated_arrival_date = Column(DateTime(timezone=True))  # ETA from SellerCloud
    received_date = Column(DateTime(timezone=True))  # Actual received date from SellerCloud
    warehouse_id = Column(UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="SET NULL"))
    
    # Lifecycle Management Fields
    date_dropped_off = Column(DateTime(timezone=True))
    door = Column(String(50))
    date_emptied = Column(DateTime(timezone=True))
    unloaded_by = Column(String(255))
    unload_cost = Column(Numeric(14, 2))
    container_cost_drayage = Column(Numeric(14, 2))
    customs_duty_misc = Column(Numeric(14, 2))
    per_diem = Column(Numeric(14, 2))
    country_of_origin = Column(String(120))
    receiving_closure_notes = Column(Text)
    factory_credit_needed = Column(Text)
    trucker_email = Column(String(255))
    last_notified_trucker_email = Column(String(255))

    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    item_links = relationship("PurchaseOrderItemContainer", back_populates="container", cascade="all, delete-orphan")
    warehouse = relationship("Warehouse", back_populates="containers")
    attachments = relationship("ShippingContainerAttachment", back_populates="container", cascade="all, delete-orphan")

class ShippingContainerAttachment(Base):
    __tablename__ = "shipping_container_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shipping_container_id = Column(UUID(as_uuid=True), ForeignKey("shipping_containers.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_url = Column(String(1024), nullable=False)
    content_type = Column(String(255))
    size = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    container = relationship("ShippingContainer", back_populates="attachments")

class PurchaseOrderItemContainer(Base):
    __tablename__ = "purchase_order_item_containers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_item_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_items.id", ondelete="CASCADE"), nullable=False)
    shipping_container_id = Column(UUID(as_uuid=True), ForeignKey("shipping_containers.id", ondelete="CASCADE"), nullable=False)
    qty_in_container = Column(Integer, default=0)
    raw_json = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    item = relationship("PurchaseOrderItem", back_populates="container_links")
    container = relationship("ShippingContainer", back_populates="item_links")


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False)
    records_synced = Column(Integer, default=0)
    message = Column(Text)
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at = Column(DateTime(timezone=True))


class TokenBlacklist(Base):
    __tablename__ = "token_blacklist"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token = Column(String(500), unique=True, nullable=False, index=True)
    blacklisted_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)

class UserActivityLog(Base):
    __tablename__ = "user_activity_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), nullable=False)  # e.g., LOGIN, SYNC_PO, CREATE_USER
    entity_type = Column(String(50), nullable=True)  # e.g., PURCHASE_ORDER, CONTAINER
    entity_id = Column(String(255), nullable=True)  # ID of the affected entity
    details = Column(JSONB, nullable=True)  # Extra context
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    user = relationship("User", backref="activity_logs")
