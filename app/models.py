import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Numeric, Integer, Text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))
    role = Column(String(50), default="user", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


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
    raw_json = Column(JSONB)
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
    is_active = Column(Boolean, default=True)
    container_lead_time_days = Column(Integer)  # days from payment/order to first container arrival, set per vendor
    raw_json = Column(JSONB)
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
    raw_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="customers")


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
    total_amount = Column(Numeric(14, 2))
    currency = Column(String(10), default="USD")
    notes = Column(Text)
    raw_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="purchase_orders")
    vendor = relationship("Vendor", back_populates="purchase_orders")
    items = relationship("PurchaseOrderItem", back_populates="purchase_order", cascade="all, delete-orphan")


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
    raw_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    purchase_order = relationship("PurchaseOrder", back_populates="items")


class ShippingContainer(Base):
    __tablename__ = "shipping_containers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sellercloud_container_id = Column(Integer, unique=True, index=True)
    container_name = Column(String(255))
    raw_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class PurchaseOrderItemContainer(Base):
    __tablename__ = "purchase_order_item_containers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_item_id = Column(UUID(as_uuid=True), ForeignKey("purchase_order_items.id", ondelete="CASCADE"), nullable=False)
    shipping_container_id = Column(UUID(as_uuid=True), ForeignKey("shipping_containers.id", ondelete="CASCADE"), nullable=False)
    qty_in_container = Column(Integer, default=0)
    raw_json = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False)
    records_synced = Column(Integer, default=0)
    message = Column(Text)
    started_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at = Column(DateTime(timezone=True))
