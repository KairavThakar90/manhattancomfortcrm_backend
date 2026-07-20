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
from datetime import datetime

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
        # link to it. Run sync_companies afterward (or first) to fill in the
        # real name/details for this row.
        company = models.Company(
            sellercloud_company_id=company_sc_id,
            name=f"Company {company_sc_id}",
        )
        db.add(company)
        db.flush()  # get company.id without full commit
    return company


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
    Confirmed structure of detail["Items"][x] (verified against a real response):
      ID, PurchaseID, ProductID (= SKU), ProductName, QtyOrdered, QtyReceived,
      QtyInContainer (null until the PO ships - not a bug), UnitPrice,
      QtyPerCase, TotalCases (= qty_cases_ordered), CostPerCase, IsKit,
      ExpectedDeliveryDate.

    Kit/bundle components: IsKit=true marks a kit item, but we haven't seen a
    real kit PO yet to confirm where its components live (possibly nested
    under a different key than "Items"). Falls back to checking li.get("Items")
    defensively - update this once a kit PO is inspected via debug_po_detail.py.
    """
    for li in items:
        db.add(models.PurchaseOrderItem(
            purchase_order_id=po_row_id,
            sellercloud_item_id=li.get("ID"),
            sku=li.get("ProductID") or li.get("SKU"),
            product_name=li.get("ProductName"),
            qty_ordered=li.get("QtyOrdered", 0),
            qty_received=li.get("QtyReceived", 0),
            qty_in_container=li.get("QtyInContainer") or 0,
            unit_price=li.get("UnitPrice", 0),
            qty_cases_ordered=li.get("TotalCases", 0),
            qty_units_per_case=li.get("QtyPerCase", 0),
            case_price=li.get("CostPerCase", 0),
            is_bundle_component=parent_item_id is not None,
            parent_sellercloud_item_id=parent_item_id,
            expected_delivery_date=li.get("ExpectedDeliveryDate"),
            raw_json=li,
        ))

        nested = li.get("Items") or []
        if nested:
            _upsert_items(db, po_row_id, nested, parent_item_id=li.get("ID"))


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
            purchase = detail.get("Purchase") or {}

            if not purchase.get("POId"):
                print(f"[sync_purchase_orders] WARNING: detail for PO {po_id} has no Purchase.POId. "
                      f"Top-level keys: {list(detail.keys())[:15]}")

            vendor = _get_or_create_vendor(db, purchase.get("VendorId"))
            company = _get_or_create_company(db, purchase.get("CompanyId"))
            mapped = _map_po(detail)
            mapped["vendor_id"] = vendor.id if vendor else None
            mapped["company_id"] = company.id if company else None

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

            line_items = detail.get("Items") or []
            if line_items:
                db.query(models.PurchaseOrderItem).filter(
                    models.PurchaseOrderItem.purchase_order_id == po.id
                ).delete()
                _upsert_items(db, po.id, line_items)

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

            for c in candidates:
                container_sc_id = c.get("ID")
                if not container_sc_id or container_sc_id in seen_container_ids:
                    continue
                seen_container_ids.add(container_sc_id)

                detail = sellercloud_client.get_container(container_sc_id)
                details_section = detail.get("Details") or {}

                container = (
                    db.query(models.ShippingContainer)
                    .filter(models.ShippingContainer.sellercloud_container_id == container_sc_id)
                    .first()
                )
                container_fields = dict(
                    sellercloud_container_id=container_sc_id,
                    container_name=details_section.get("ContainerName"),
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

            db.commit()

        print(f"[sync_containers] done. {containers_synced} containers, {links_synced} item links.")
        _log_sync(db, "shipping_containers", "success", containers_synced)
    except Exception as e:
        db.rollback()
        print(f"[sync_containers] FAILED after {containers_synced} containers: {e}")
        _log_sync(db, "shipping_containers", "failed", containers_synced, str(e))
        raise

    return {"containers_synced": containers_synced, "links_synced": links_synced}