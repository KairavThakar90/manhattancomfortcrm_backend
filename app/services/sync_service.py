"""
Pulls data from SellerCloud and upserts it into our Neon Postgres tables.

Each sync_* function:
  1. Pages through the SellerCloud endpoint
  2. Upserts by sellercloud_*_id (insert new, update existing)
  3. Logs the run in sync_logs

Adjust the field-mapping dicts (`_map_company`, `_map_vendor`, etc.) once you
confirm exact response field names from your Swagger UI - the keys on the
right-hand side (e.g. row.get("Name")) are the SellerCloud response fields.
"""
from datetime import datetime, timezone
import uuid
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from app import models
from app.services.sellercloud_client import sellercloud_client


def _log_sync(db: Session, entity_type: str, status: str, count: int, message: str = ""):
    log = models.SyncLog(
        entity_type=entity_type,
        status=status,
        records_synced=count,
        message=message,
        finished_at=datetime.utcnow(),
    )
    db.add(log)
    db.commit()


# ---------------- Companies ----------------
def _map_company(row: dict) -> dict:
    return dict(
        sellercloud_company_id=row.get("ID"),
        name=row.get("Name") or row.get("CompanyName") or "Unnamed",
        email=row.get("Email"),
        phone=row.get("Phone"),
        city=row.get("City"),
        state=row.get("State"),
        postal_code=row.get("PostalCode") or row.get("Zip"),
        country=row.get("Country"),
        is_active=row.get("IsActive", True),
        raw_json=row,
    )


# ---------------- Vendors ----------------
def _map_vendor(row: dict) -> dict:
    """
    Maps SellerCloud vendor response to local vendor fields.
    
    Confirmed API response from GET /api/Vendors and GET /api/Vendors/{id}:
      - ID: vendor ID
      - Name: vendor name
      - Email: primary email
      - EmailCC: CC email
      - Alias: vendor alias
      - AccountNumber: account number
      - Website: vendor website
      - IsActive: active status
      - IsDefault: default vendor flag
    
    Note: SellerCloud Vendor API does NOT provide address or phone fields.
    """
    return dict(
        sellercloud_vendor_id=row.get("ID"),
        name=row.get("Name") or "Unnamed Vendor",
        email=row.get("Email"),
        phone=None,  # Not available in SellerCloud Vendor API
        address_line1=None,  # Not available in SellerCloud Vendor API
        city=None,  # Not available in SellerCloud Vendor API
        state=None,  # Not available in SellerCloud Vendor API
        postal_code=None,  # Not available in SellerCloud Vendor API
        country=None,  # Not available in SellerCloud Vendor API
        is_active=row.get("IsActive", True),
        raw_json=row,
    )


def sync_vendors(db: Session) -> int:
    """
    Pulls vendors from SellerCloud and upserts them into the vendors table.
    This should be run periodically to ensure vendor names and contact info
    are up-to-date, not just the stub records created during PO sync.
    """
    synced = 0
    try:
        page = 1
        while True:
            data = sellercloud_client.get_vendors(page_number=page, page_size=100)
            items = data.get("Items") or data.get("items") or data
            if not items:
                break

            for row in items:
                mapped = _map_vendor(row)
                existing = (
                    db.query(models.Vendor)
                    .filter(models.Vendor.sellercloud_vendor_id == mapped["sellercloud_vendor_id"])
                    .first()
                )
                if existing:
                    for k, v in mapped.items():
                        # Do not overwrite locally populated fields with None from SellerCloud
                        if v is not None or getattr(existing, k) is None:
                            setattr(existing, k, v)
                else:
                    db.add(models.Vendor(**mapped))
                synced += 1

            db.commit()
            if len(items) < 100:
                break
            page += 1

        _log_sync(db, "vendors", "success", synced)
    except Exception as e:
        db.rollback()
        _log_sync(db, "vendors", "failed", synced, str(e))
        raise
    return synced


def sync_companies(db: Session) -> int:
    synced = 0
    try:
        page = 1
        while True:
            data = sellercloud_client.get_companies(page_number=page, page_size=100)
            items = data.get("Items") or data.get("items") or data
            if not items:
                break

            for row in items:
                mapped = _map_company(row)
                existing = (
                    db.query(models.Company)
                    .filter(models.Company.sellercloud_company_id == mapped["sellercloud_company_id"])
                    .first()
                )
                if existing:
                    for k, v in mapped.items():
                        setattr(existing, k, v)
                else:
                    db.add(models.Company(**mapped))
                synced += 1

            db.commit()
            if len(items) < 100:
                break
            page += 1

        _log_sync(db, "companies", "success", synced)
    except Exception as e:
        db.rollback()
        _log_sync(db, "companies", "failed", synced, str(e))
        raise
    return synced


def sync_warehouses(db: Session) -> int:
    client = sellercloud_client
    try:
        data = client.get_warehouses()
    except Exception as e:
        print(f"Failed to fetch warehouses from SC: {e}")
        return 0

    items = data.get("Items", [])
    if not items:
        return 0

    count = 0
    for w in items:
        w_id = w.get("ID")
        if not w_id:
            continue
            
        existing = db.query(models.Warehouse).filter(models.Warehouse.sellercloud_warehouse_id == w_id).first()
        if existing:
            existing.name = w.get("Name")
            existing.is_default = w.get("IsDefault", False)
            existing.warehouse_type = w.get("WarehouseType")
            existing.is_sellable = w.get("IsSellAble", True)
        else:
            db.add(models.Warehouse(
                sellercloud_warehouse_id=w_id,
                name=w.get("Name"),
                is_default=w.get("IsDefault", False),
                warehouse_type=w.get("WarehouseType"),
                is_sellable=w.get("IsSellAble", True)
            ))
        count += 1
    
    db.commit()
    return count


# ---------------- Customers ----------------
def _map_customer(row: dict) -> dict:
    return dict(
        sellercloud_customer_id=row.get("UserID"),
        first_name=row.get("FirstName"),
        last_name=row.get("LastName"),
        email=row.get("Email"),
        # Other fields could be added if needed based on API response
        raw_json=row,
    )

def sync_customers(db: Session) -> int:
    synced = 0
    try:
        page = 1
        while True:
            data = sellercloud_client.get_customers(page_number=page, page_size=100)
            items = data.get("Items") or data.get("items") or data
            if not items:
                break

            for row in items:
                mapped = _map_customer(row)
                existing = (
                    db.query(models.Customer)
                    .filter(models.Customer.sellercloud_customer_id == mapped["sellercloud_customer_id"])
                    .first()
                )
                if existing:
                    for k, v in mapped.items():
                        setattr(existing, k, v)
                else:
                    db.add(models.Customer(**mapped))
                synced += 1

            db.commit()
            if len(items) < 100:
                break
            page += 1

        _log_sync(db, "customers", "success", synced)
    except Exception as e:
        db.rollback()
        _log_sync(db, "customers", "failed", synced, str(e))
        raise
    return synced


def _get_customer_id_from_order_detail(db: Session, order_detail: dict) -> Optional[uuid.UUID]:
    # 1. Try by OrderDetails.CustomerID
    sc_customer_id = None
    order_details_block = order_detail.get("OrderDetails", {})
    if order_details_block:
        sc_customer_id = order_details_block.get("CustomerID")
        
    if not sc_customer_id or sc_customer_id == 0:
        # Fallback to older locations just in case
        sc_customer_id = order_detail.get("UserID") or order_detail.get("Customer", {}).get("ID")
        
    if sc_customer_id and sc_customer_id != 0:
        # Check if we already have this customer in DB
        customer = db.query(models.Customer).filter(models.Customer.sellercloud_customer_id == sc_customer_id).first()
        if customer:
            return customer.id
            
        # We don't have it, let's fetch it from SellerCloud
        try:
            from app.services.sellercloud_client import sellercloud_client
            sc_cust = sellercloud_client._request("GET", f"/api/Customers/{sc_customer_id}").json()
            gen = sc_cust.get("General", {})
            
            # Use Addresses block to get accurate billing info
            addresses = sc_cust.get("Addresses", [])
            billing = next((a for a in addresses if a.get("IsBillingAddress")), {})
            if not billing and addresses:
                billing = addresses[0]
                
            new_customer = models.Customer(
                sellercloud_customer_id=sc_customer_id,
                first_name=gen.get("FirstName") or billing.get("FirstName", ""),
                last_name=gen.get("CorporateName") or gen.get("LastName") or billing.get("CompanyName", ""),
                email=gen.get("Email") or billing.get("EmailAddress", ""),
                phone=billing.get("Phone") or billing.get("PhoneNumber", ""),
                billing_address_line1=billing.get("Address") or billing.get("StreetLine1", ""),
                billing_city=billing.get("City", ""),
                billing_state=billing.get("State", "") or billing.get("StateCode", ""),
                billing_postal_code=billing.get("ZipCode", "") or billing.get("PostalCode", ""),
                billing_country=billing.get("Country", "") or billing.get("CountryCode", "")
            )
            db.add(new_customer)
            db.flush()
            return new_customer.id
        except Exception as e:
            print(f"Error fetching customer {sc_customer_id} from SC: {e}")
            # Fall back to creating from order detail below
            
    # 2. Try by Email or Name from Order's BillingAddress
    billing = order_detail.get("BillingAddress") or {}
    email = billing.get("EmailAddress")
    
    customer = None
    if email:
        customer = db.query(models.Customer).filter(models.Customer.email.ilike(email)).first()
        
    if not customer:
        company = billing.get("CompanyName")
        last_name = billing.get("LastName")
        first_name = billing.get("FirstName")
        
        from sqlalchemy import or_
        
        # Try to match by company or last name if no email
        if company:
            # We map company name to last_name, so search for it there
            customer = db.query(models.Customer).filter(
                or_(
                    models.Customer.last_name.ilike(company),
                    models.Customer.last_name.ilike(last_name) if last_name else False
                )
            ).first()
        elif last_name:
            customer = db.query(models.Customer).filter(
                models.Customer.last_name.ilike(last_name),
                models.Customer.first_name.ilike(first_name) if first_name else True
            ).first()
            
    if customer:
        return customer.id
    else:
        # Auto-create the customer so it can be linked
        new_customer = models.Customer(
            first_name=billing.get("FirstName", ""),
            last_name=billing.get("CompanyName") or billing.get("LastName", ""),
            email=email or "",
            phone=billing.get("PhoneNumber", ""),
            billing_address_line1=billing.get("StreetLine1", ""),
            billing_city=billing.get("City", ""),
            billing_state=billing.get("StateCode", ""),
            billing_postal_code=billing.get("PostalCode", ""),
            billing_country=billing.get("CountryCode", "")
        )
        db.add(new_customer)
        db.flush()
        return new_customer.id

def _get_or_create_channel(db: Session, channel_name: str) -> Optional[models.Channel]:
    if not channel_name:
        return None
    channel_name = str(channel_name)
    channel = db.query(models.Channel).filter(models.Channel.name == channel_name).first()
    if not channel:
        import uuid
        channel = models.Channel(id=uuid.uuid4(), name=channel_name)
        db.add(channel)
        db.flush()
    return channel

ORDER_SOURCE_MAP = {
    0: "Wholesale",
    1: "Amazon",
    2: "eBay",
    4: "Website",
    5: "Magento",
    8: "Overstock",
    11: "Target",
    14: "Newegg",
    21: "Wayfair",
    23: "Walmart",
    24: "Shopify",
    32: "Jet",
    44: "Houzz",
    62: "Home Depot",
    109: "Macy's",
    114: "Lowe's"
}

def _get_channel_name_from_order(order_detail: dict) -> str:
    """Extract human-readable channel name from SellerCloud order JSON."""
    if order_detail.get("IsWholeSaleOrder"):
        return "Wholesale"
        
    order_source = order_detail.get("OrderDetails", {}).get("OrderSource")
    if order_source is not None:
        try:
            source_int = int(order_source)
            return ORDER_SOURCE_MAP.get(source_int, str(source_int))
        except (ValueError, TypeError):
            return str(order_source)
            
    return "Unknown"

def _extract_order_info_from_po_detail(db: Session, detail: dict) -> dict:
    """Extracts OrderID from PO detail, fetches Order, and returns customer_id, channel_order_id, channel_id."""
    related_items = detail.get("RelatedItems") or []
    order_id = None
    for item in related_items:
        if item.get("RecordType") == "Order":
            order_id = item.get("ID")
            break
            
    if not order_id:
        return {"customer_id": None, "channel_order_id": None, "channel_id": None}
        
    try:
        from app.services.sellercloud_client import sellercloud_client
        order_detail = sellercloud_client.get_order(order_id)
        
        customer_id = _get_customer_id_from_order_detail(db, order_detail)
        
        order_details_block = order_detail.get("OrderDetails", {})
        channel_order_id = order_details_block.get("OrderSourceOrderId")
        channel_name = _get_channel_name_from_order(order_detail)
        
        channel_id = None
        if channel_name and channel_name != "Unknown":
            channel = _get_or_create_channel(db, channel_name)
            if channel:
                channel_id = channel.id
        
        return {
            "customer_id": customer_id,
            "channel_order_id": channel_order_id,
            "channel_id": channel_id
        }
    except Exception as e:
        print(f"Error fetching order info for Order {order_id}: {e}")
        return {"customer_id": None, "channel_order_id": None, "channel_id": None}


def backfill_po_customers(db: Session):
    """One-time background task to backfill customer_id for all existing POs."""
    import time
    from sqlalchemy import or_
    pos = db.query(models.PurchaseOrder).filter(
        or_(
            models.PurchaseOrder.customer_id == None,
            models.PurchaseOrder.channel_id == None,
            models.PurchaseOrder.channel_order_id == None
        )
    ).all()
    total_pos = len(pos)
    print(f"Starting backfill for {total_pos} POs...")
    count = 0
    for idx, po in enumerate(pos):
        try:
            print(f"[{idx + 1}/{total_pos}] Processing PO {po.sellercloud_po_id}...")
            
            # We already have the raw JSON from the listing API or previous sync
            detail = po.raw_json or {}
            
            # Use existing function which looks for RelatedItems in the dict
            order_info = _extract_order_info_from_po_detail(db, detail)
            customer_id = order_info["customer_id"]
            channel_order_id = order_info["channel_order_id"]
            channel_id = order_info["channel_id"]
            
            if not customer_id and po.purchase_title:
                # Fallback: Extract from "Created for Order# XXXXX"
                import re
                match = re.search(r'Order#\s*(\d+)', po.purchase_title, re.IGNORECASE)
                if match:
                    order_id = match.group(1)
                    try:
                        from app.services.sellercloud_client import sellercloud_client
                        order_detail = sellercloud_client.get_order(order_id)
                        customer_id = _get_customer_id_from_order_detail(db, order_detail)
                        order_details_block = order_detail.get("OrderDetails", {})
                        if not channel_order_id:
                            channel_order_id = order_details_block.get("OrderSourceOrderId")
                        if not channel_id:
                            channel_name = _get_channel_name_from_order(order_detail)
                            if channel_name:
                                channel = _get_or_create_channel(db, channel_name)
                                if channel:
                                    channel_id = channel.id
                    except Exception as e:
                        print(f"Fallback order fetch failed for PO {po.sellercloud_po_id}: {e}")
            
            updated = False
            if customer_id and not po.customer_id:
                po.customer_id = customer_id
                updated = True
            if channel_order_id and not po.channel_order_id:
                po.channel_order_id = channel_order_id
                updated = True
            if channel_id and not po.channel_id:
                po.channel_id = channel_id
                updated = True
                
            if updated:
                db.commit()
                count += 1
            
            # Avoid hammering the API and causing SSL timeouts
            time.sleep(0.5)
            
        except Exception as e:
            print(f"Failed processing PO {po.sellercloud_po_id}: {e}")
            db.rollback()
            
    print(f"Backfill complete! Updated {count} POs with customer links.")


# ---------------- Purchase Orders ----------------
#
# Confirmed real field names from the working Apps Script:
#   List (GetAllByView): ID, VendorID, PurchaseTitle, CreatedOn, DateOrdered,
#                         PurchaseOrderStatus (int), ReceivingStatus (int),
#                         Invoices[0].InvoiceDate, Items[].SKU, Items[].ID,
#                         Items[].QtyOrdered, Items[].QtyReceived,
#                         Items[].Items[] (bundle/kit components)
#   Detail (/PurchaseOrders/{id}): same shape, PLUS Items[].QtyInContainer,
#                         which is NOT present on the list response.
#
# PurchaseOrderStatus / ReceivingStatus are SellerCloud enum ints. We store the
# raw code always (safe), and leave status_label for you to fill in once you've
# confirmed the enum mapping from Swagger (Admin > Purchase Orders in the SC UI
# usually shows the text next to each code, e.g. via the dropdown filter).

def _map_po(detail: dict) -> dict:
    """
    Confirmed structure of GET /PurchaseOrders/{id} (nested, NOT flat like the
    list/GetAllByView response) - verified against a real response:
      detail["Purchase"]          -> POId, VendorId, CompanyId, Description
                                      (=PurchaseTitle), OrderedOn, ExpectedDelivery
      detail["VendorAndInvoice"]  -> InvoiceDate, Memo, Invoices[]
      detail["Changes"]           -> CreatedOn, UpdatedOn
      detail["Statuses"]          -> Status (=PurchaseOrderStatus), ReceivingStatus,
                                      PaymentStatus, ShippingStatus
      detail["TotalInfo"]         -> GrandTotal, SubTotal, TaxTotal, etc.
      detail["Items"]             -> line items (see _upsert_items)
    """
    purchase = detail.get("Purchase") or {}
    vendor_invoice = detail.get("VendorAndInvoice") or {}
    changes = detail.get("Changes") or {}
    statuses = detail.get("Statuses") or {}
    total_info = detail.get("TotalInfo") or {}

    invoices = vendor_invoice.get("Invoices") or []
    invoice_date = vendor_invoice.get("InvoiceDate") or (invoices[0].get("InvoiceDate") if invoices else None)

    return dict(
        sellercloud_po_id=purchase.get("POId"),
        purchase_title=purchase.get("Description"),
        purchase_order_status_code=statuses.get("Status"),
        receiving_status_code=statuses.get("ReceivingStatus"),
        created_on=changes.get("CreatedOn"),
        date_ordered=purchase.get("OrderedOn"),
        expected_delivery_date=purchase.get("ExpectedDelivery"),
        invoice_date=invoice_date,
        total_amount=total_info.get("GrandTotal") or 0,
        currency="USD",
        notes=vendor_invoice.get("Memo") or purchase.get("Instructions"),
        sellercloud_warehouse_id=purchase.get("DefaultWarehouseID"),
        raw_json=detail,
    )


def _get_or_create_company(db: Session, company_sc_id):
    if not company_sc_id:
        return None
    company = (
        db.query(models.Company)
        .filter(models.Company.sellercloud_company_id == company_sc_id)
        .first()
    )
    if not company:
        # Not seen via sync_companies yet - create a stub so the PO can still
        # be linked successfully.
        company = models.Company(
            sellercloud_company_id=company_sc_id,
            name=f"Company {company_sc_id} (Unsynced)",
        )
        db.add(company)
        db.flush()  # get company.id without full commit
    return company


def _get_or_create_warehouse(db: Session, warehouse_sc_id):
    if not warehouse_sc_id:
        return None
    warehouse = (
        db.query(models.Warehouse)
        .filter(models.Warehouse.sellercloud_warehouse_id == warehouse_sc_id)
        .first()
    )
    if not warehouse:
        warehouse = models.Warehouse(
            sellercloud_warehouse_id=warehouse_sc_id,
            name=f"Warehouse {warehouse_sc_id} (Unsynced)",
        )
        db.add(warehouse)
        db.flush()
    return warehouse


def _get_or_create_vendor(db: Session, vendor_sc_id):
    if not vendor_sc_id:
        return None
    vendor = (
        db.query(models.Vendor)
        .filter(models.Vendor.sellercloud_vendor_id == vendor_sc_id)
        .first()
    )
    if not vendor:
        vendor = models.Vendor(
            sellercloud_vendor_id=vendor_sc_id,
            name=f"Vendor {vendor_sc_id}",  # list view only gives VendorID, no name - backfill separately via /api/Vendors/{id} if you need the name
        )
        db.add(vendor)
        db.flush()  # get vendor.id without full commit
    return vendor


def _upsert_items(db: Session, po_row_id, items: list, parent_item_id=None):
    """
    Upserts PO line items — UPDATE existing rows matched by sellercloud_item_id,
    INSERT new ones.  This replaces the old delete-all-then-insert pattern so that
    PurchaseOrderItemContainer rows (container links) are NEVER wiped when a PO is
    re-synced.

    Confirmed structure of detail["Items"][x]:
      ID, PurchaseID, ProductID (= SKU), ProductName, QtyOrdered, QtyReceived,
      QtyInContainer, UnitPrice, QtyPerCase, TotalCases, CostPerCase, IsKit,
      ExpectedDeliveryDate.
    """
    for li in items:
        sc_item_id = li.get("ID")
        existing = None
        if sc_item_id:
            existing = (
                db.query(models.PurchaseOrderItem)
                .filter(
                    models.PurchaseOrderItem.purchase_order_id == po_row_id,
                    models.PurchaseOrderItem.sellercloud_item_id == sc_item_id,
                )
                .first()
            )

        fields = dict(
            purchase_order_id=po_row_id,
            sellercloud_item_id=sc_item_id,
            sku=li.get("ProductID") or li.get("SKU"),
            product_name=li.get("ProductName"),
            qty_ordered=li.get("QtyOrdered", 0),
            qty_received=li.get("QtyReceived", 0),
            # Only overwrite qty_in_container from SC if SC reports > 0.
            # If SC returns 0/null it might just mean "not shipped yet" —
            # we keep our locally-tracked value so container links stay accurate.
            qty_in_container=li.get("QtyInContainer") or 0,
            unit_price=li.get("UnitPrice", 0),
            qty_cases_ordered=li.get("TotalCases", 0),
            qty_units_per_case=li.get("QtyPerCase", 0),
            case_price=li.get("CostPerCase", 0),
            is_bundle_component=parent_item_id is not None,
            parent_sellercloud_item_id=parent_item_id,
            expected_delivery_date=(
                datetime.fromisoformat(li.get("ExpectedDeliveryDate").replace("Z", "")).replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                if li.get("ExpectedDeliveryDate") else None
            ),
            raw_json=li,
        )

        if existing:
            # UPDATE — preserve the row so container_links FK is not broken
            for k, v in fields.items():
                if k != "purchase_order_id":  # never change parent FK
                    setattr(existing, k, v)
        else:
            db.add(models.PurchaseOrderItem(**fields))

        nested = li.get("Items") or []
        if nested:
            _upsert_items(db, po_row_id, nested, parent_item_id=sc_item_id)


def sync_purchase_orders(db: Session, view_id: int = None, max_pages: int = 100) -> int:
    """
    Two-pass sync, mirroring your Apps Script:
      1. Page through GetAllByView to collect PO IDs (cheap, filtered by the saved view).
      2. Fetch each PO's full detail to get complete + accurate item data
         (incl. QtyInContainer, which the list view omits).
    """
    synced = 0
    try:
        po_ids = []
        page = 1
        while page <= max_pages:
            data = sellercloud_client.get_purchase_orders_by_view(
                view_id=view_id, page_number=page, page_size=50
            )
            items = data.get("Items") or []
            print(f"[sync_purchase_orders] page {page}: {len(items)} items returned")
            if not items:
                break
            po_ids.extend([row.get("ID") for row in items if row.get("ID")])
            if len(items) < 50:
                break
            page += 1

        print(f"[sync_purchase_orders] total PO IDs collected across all pages: {len(po_ids)}")
        
        for po_id in po_ids:
            detail = sellercloud_client.get_purchase_order(po_id)
            
            mapped = _map_po(detail)
            
            # Map foreign keys
            purchase = detail.get("Purchase") or {}
            vendor = _get_or_create_vendor(db, purchase.get("VendorId"))
            company = _get_or_create_company(db, purchase.get("CompanyId"))
            
            warehouse_sc_id = mapped.pop("sellercloud_warehouse_id", None)
            warehouse = _get_or_create_warehouse(db, warehouse_sc_id)
            
            mapped["vendor_id"] = vendor.id if vendor else None
            mapped["company_id"] = company.id if company else None
            mapped["warehouse_id"] = warehouse.id if warehouse else None

            if "Description" not in purchase:
                print(f"[sync_purchase_orders] WARNING: PO {po_id} - 'Description' key missing entirely from "
                      f"Purchase object. Purchase keys: {list(purchase.keys())[:20]}")

            existing = (
                db.query(models.PurchaseOrder)
                .filter(models.PurchaseOrder.sellercloud_po_id == mapped["sellercloud_po_id"])
                .first()
            )
            if existing:
                for k, v in mapped.items():
                    setattr(existing, k, v)
                po = existing
            else:
                po = models.PurchaseOrder(**mapped)
                db.add(po)
                db.flush()

            # Fetch and set customer & channel info dynamically if missing
            if not po.customer_id or not po.channel_order_id or not po.channel_id:
                order_info = _extract_order_info_from_po_detail(db, detail)
                
                if order_info["customer_id"] and not po.customer_id:
                    po.customer_id = order_info["customer_id"]
                if order_info["channel_order_id"] and not po.channel_order_id:
                    po.channel_order_id = order_info["channel_order_id"]
                if order_info["channel_id"] and not po.channel_id:
                    po.channel_id = order_info["channel_id"]
                    
                # Fallback to Title if info still missing
                if (not po.customer_id or not po.channel_order_id) and po.purchase_title:
                    import re
                    match = re.search(r'Order#\s*(\d+)', po.purchase_title, re.IGNORECASE)
                    if match:
                        order_id = match.group(1)
                        try:
                            order_detail = sellercloud_client.get_order(order_id)
                            if not po.customer_id:
                                po.customer_id = _get_customer_id_from_order_detail(db, order_detail)
                            order_details_block = order_detail.get("OrderDetails", {})
                            if not po.channel_order_id:
                                po.channel_order_id = order_details_block.get("OrderSourceOrderId")
                            if not po.channel_id:
                                channel_name = _get_channel_name_from_order(order_detail)
                                if channel_name:
                                    channel = _get_or_create_channel(db, channel_name)
                                    if channel:
                                        po.channel_id = channel.id
                        except Exception as e:
                            print(f"Fallback order fetch failed for PO {po.sellercloud_po_id}: {e}")

            line_items = detail.get("Items") or []
            if line_items:
                # Use upsert (not delete+insert) so PurchaseOrderItemContainer
                # rows (container links) are never wiped on re-sync.
                _upsert_items(db, po.id, line_items)
                
                # Auto-update shipment status
                from app.services.po_service import recalculate_po_shipment_status
                recalculate_po_shipment_status(db, str(po.id))

            db.commit()
            synced += 1

        print(f"[sync_purchase_orders] done. {synced} POs synced.")
        _log_sync(db, "purchase_orders", "success", synced)
    except Exception as e:
        db.rollback()
        print(f"[sync_purchase_orders] FAILED after {synced} POs: {e}")
        _log_sync(db, "purchase_orders", "failed", synced, str(e))
        raise
    return synced


# ---------------- Shipping Containers ----------------
#
# Confirmed real shape from debug_shipping_container.py:
#   Step 1 (GET /ShippingContainers?model.poIds=X&model.productIds=Y):
#     {"Items": [{ID, ContainerName, EstimatedArrivalDate, ReceivedDate, ...}], "TotalResults": N}
#   Step 2 (GET /ShippingContainers/{id}):
#     {"Details": {ContainerName, EstimatedArrivalDate, ReceivedOnDate, ...},
#      "Items": {"Results": [{ID, POItemID, Qty, QtyReceived, POID, ProductID, ...}], "TotalResults": N}}
#
# IMPORTANT: a single container's Items.Results can include line items from
# MULTIPLE different POs (containers get consolidated). So one container fetch
# can populate links for many purchase_order_items at once - we take advantage
# of that below instead of re-fetching the same container repeatedly.

def sync_containers(db: Session, po_id: int = None) -> dict:
    """
    For every purchase_order_item (optionally scoped to one PO via po_id) that
    has a SKU, discovers its container(s) via step 1, then fetches each new
    container's full detail via step 2 and links every matching item found
    inside it - not just the one we searched for.

    Scoping to a single po_id is recommended for on-demand use; running with
    po_id=None will make one ShippingContainers call per (PO, SKU) pair across
    your whole database, which can be a lot of SellerCloud API calls.
    """
    query = db.query(models.PurchaseOrderItem).join(models.PurchaseOrder).filter(
        models.PurchaseOrderItem.sku.isnot(None)
    )
    if po_id:
        query = query.filter(models.PurchaseOrder.sellercloud_po_id == po_id)
    items = query.all()

    item_by_sc_id = {it.sellercloud_item_id: it for it in items if it.sellercloud_item_id}

    seen_container_ids = set()
    checked_pairs = set()
    containers_synced = 0
    links_synced = 0

    try:
        for it in items:
            po = it.purchase_order
            pair = (po.sellercloud_po_id, it.sku)
            if pair in checked_pairs or not po.sellercloud_po_id:
                continue
            checked_pairs.add(pair)

            resp = sellercloud_client.get_containers_for_po_product(po.sellercloud_po_id, it.sku)
            candidates = resp.get("Items") or []

            valid_sc_ids = set()
            for c in candidates:
                if c.get("ID"):
                    valid_sc_ids.add(c.get("ID"))

            # CLEANUP: Remove local links for this PO item if they no longer exist in SellerCloud
            try:
                local_links = db.query(models.PurchaseOrderItemContainer).filter(
                    models.PurchaseOrderItemContainer.purchase_order_item_id == it.id
                ).all()
                for link in local_links:
                    if link.container and link.container.sellercloud_container_id is not None:
                        if link.container.sellercloud_container_id not in valid_sc_ids:
                            db.delete(link)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"Error cleaning up orphan links for item {it.id}: {e}")

            for c in candidates:
                container_sc_id = c.get("ID")
                if not container_sc_id or container_sc_id in seen_container_ids:
                    continue
                seen_container_ids.add(container_sc_id)

                try:
                    detail = sellercloud_client.get_container(container_sc_id)
                    details_section = detail.get("Details") or {}

                    # Look up by SC container ID first
                    container = (
                        db.query(models.ShippingContainer)
                        .filter(models.ShippingContainer.sellercloud_container_id == container_sc_id)
                        .first()
                    )
                    # Fallback: if we created this container locally but the SC sync
                    # failed, the local row has sellercloud_container_id=NULL but the
                    # same name.  Match by name to avoid creating a duplicate row.
                    if not container:
                        sc_name = details_section.get("ContainerName")
                        if sc_name:
                            container = (
                                db.query(models.ShippingContainer)
                                .filter(
                                    models.ShippingContainer.container_name == sc_name,
                                    models.ShippingContainer.sellercloud_container_id.is_(None),
                                )
                                .first()
                            )

                    # Sellercloud sometimes returns ReceivingWarehouseID and sometimes ReceiveWarehouseID
                    warehouse_sc_id = details_section.get("ReceivingWarehouseID") or details_section.get("ReceiveWarehouseID")
                    warehouse = _get_or_create_warehouse(db, warehouse_sc_id)

                    received_date = None
                    recv_raw = details_section.get("ReceivedOnDate") or details_section.get("ReceivedDate")
                    if recv_raw:
                        try:
                            raw_dt = datetime.fromisoformat(recv_raw.replace("Z", ""))
                            received_date = raw_dt.replace(hour=12, minute=0, second=0, tzinfo=timezone.utc)
                        except ValueError:
                            pass

                    container_fields = dict(
                        sellercloud_container_id=container_sc_id,
                        container_name=details_section.get("ContainerName"),
                        received_date=received_date,
                        warehouse_id=warehouse.id if warehouse else None,
                        raw_json=detail,
                    )
                    if container:
                        for k, v in container_fields.items():
                            setattr(container, k, v)
                    else:
                        container = models.ShippingContainer(**container_fields)
                        db.add(container)
                        db.flush()
                    containers_synced += 1

                    results = ((detail.get("Items") or {}).get("Results")) or []
                    for entry in results:
                        match = item_by_sc_id.get(entry.get("POItemID"))
                        if not match:
                            continue  # this container item belongs to a PO/item we haven't synced locally - skip

                        existing_link = (
                            db.query(models.PurchaseOrderItemContainer)
                            .filter(
                                models.PurchaseOrderItemContainer.purchase_order_item_id == match.id,
                                models.PurchaseOrderItemContainer.shipping_container_id == container.id,
                            )
                            .first()
                        )
                        link_fields = dict(
                            purchase_order_item_id=match.id,
                            shipping_container_id=container.id,
                            qty_in_container=entry.get("Qty", 0),
                            raw_json=entry,
                        )
                        if existing_link:
                            for k, v in link_fields.items():
                                setattr(existing_link, k, v)
                        else:
                            db.add(models.PurchaseOrderItemContainer(**link_fields))
                        links_synced += 1

                    # Commit each container immediately so errors in subsequent containers don't rollback the good ones
                    db.commit()
                except Exception as inner_e:
                    db.rollback()
                    print(f"Error syncing individual container {container_sc_id}: {inner_e}")
                    continue


        print(f"[sync_containers] done. {containers_synced} containers, {links_synced} item links.")
        _log_sync(db, "shipping_containers", "success", containers_synced)
        
        # CLEANUP: Delete containers that have no remaining item links (i.e., completely deleted in SC)
        try:
            orphan_containers = db.query(models.ShippingContainer).outerjoin(
                models.PurchaseOrderItemContainer, 
                models.ShippingContainer.id == models.PurchaseOrderItemContainer.shipping_container_id
            ).filter(
                models.PurchaseOrderItemContainer.id == None,
                models.ShippingContainer.sellercloud_container_id.isnot(None)
            ).all()
            for oc in orphan_containers:
                db.delete(oc)
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"Error cleaning up orphan containers: {e}")
            
    except Exception as e:
        db.rollback()
        print(f"[sync_containers] FAILED after {containers_synced} containers: {e}")
        _log_sync(db, "shipping_containers", "failed", containers_synced, str(e))
        raise

    return {"containers_synced": containers_synced, "links_synced": links_synced}


def sync_containers_for_all_pos(db: Session, limit: int = None) -> dict:
    """
    Sync containers for all (or limited number of) purchase orders.
    
    This function processes POs one by one, calling sync_containers for each.
    It's more efficient than the original sync_containers(po_id=None) because
    it processes POs in batches and provides progress tracking.
    
    Args:
        db: Database session
        limit: Maximum number of POs to process (for testing). None = all POs
    
    Returns:
        dict with:
        - pos_processed: Number of POs that were checked
        - containers_synced: Total containers created/updated
        - links_synced: Total item-container links created/updated
    """
    # Get all POs with items
    query = (
        db.query(models.PurchaseOrder)
        .join(models.PurchaseOrderItem)
        .filter(models.PurchaseOrderItem.sku.isnot(None))
        .distinct()
        .order_by(models.PurchaseOrder.sellercloud_po_id.desc())
    )
    
    if limit:
        query = query.limit(limit)
    
    pos = query.all()
    
    print(f"[sync_containers_for_all_pos] Processing {len(pos)} POs...")
    
    total_containers = 0
    total_links = 0
    pos_processed = 0
    
    try:
        for po in pos:
            if not po.sellercloud_po_id:
                continue
            
            try:
                result = sync_containers(db, po_id=po.sellercloud_po_id)
                total_containers += result["containers_synced"]
                total_links += result["links_synced"]
                pos_processed += 1
                
                if pos_processed % 10 == 0:
                    print(f"[sync_containers_for_all_pos] Processed {pos_processed}/{len(pos)} POs...")
                    
            except Exception as e:
                print(f"[sync_containers_for_all_pos] Error processing PO {po.sellercloud_po_id}: {e}")
                # Continue with next PO instead of failing completely
                continue
        
        print(f"[sync_containers_for_all_pos] DONE. {pos_processed} POs, {total_containers} containers, {total_links} links.")
        _log_sync(db, "shipping_containers_bulk", "success", total_containers, 
                  f"Processed {pos_processed} POs, created/updated {total_containers} containers, {total_links} links")
        
    except Exception as e:
        print(f"[sync_containers_for_all_pos] FAILED after {pos_processed} POs: {e}")
        _log_sync(db, "shipping_containers_bulk", "failed", total_containers, str(e))
        raise
    
    return {
        "pos_processed": pos_processed,
        "containers_synced": total_containers,
        "links_synced": total_links
    }
