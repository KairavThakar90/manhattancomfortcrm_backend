"""
Container API endpoints
"""
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import get_current_user
from app import models, schemas
from app.schemas import (
    ContainerOut, ContainerCreate, POItemsForContainerResponse,
    ContainerListResponse, ContainerDetailOut, ContainerDetailItemOut,
    ContainerUpdate, ContainerAddItems
)
from app.services.sellercloud_client import SellerCloudClient
from app.services.activity_service import log_activity

import uuid


def format_sc_date(dt):
    if not dt: return None
    return dt.strftime("%Y-%m-%dT12:00:00Z")

def resolve_container_filter(container_id: str):
    """
    Helper to allow fetching a container by either its local UUID or its SellerCloud integer ID.
    Returns the SQLAlchemy filter condition.
    """
    try:
        container_uuid = uuid.UUID(container_id)
        return models.ShippingContainer.id == container_uuid
    except ValueError:
        if container_id.isdigit():
            return models.ShippingContainer.sellercloud_container_id == int(container_id)
        raise HTTPException(status_code=400, detail="Invalid container ID format (must be UUID or SellerCloud integer ID)")

router = APIRouter(
    prefix="/containers",
    tags=["Containers"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------------
# GET /containers/  — paginated list with filters
# ---------------------------------------------------------------------------
@router.get("")
@router.get("/")
def list_containers(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(25, ge=1, le=200, description="Items per page"),
    received: Optional[bool] = Query(None, description="True = received only, False = not received, omit = all"),
    search: Optional[str] = Query(None, description="Search by container name (partial match)"),
    po_id: Optional[str] = Query(None, description="Filter by Purchase Order UUID"),
    sellercloud_po_id: Optional[int] = Query(None, description="Filter by SellerCloud PO integer ID"),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    sellercloud_warehouse_id: Optional[int] = Query(None, description="Filter by SellerCloud Warehouse ID"),
    date_from: Optional[datetime] = Query(None, description="Filter containers received on or after this date"),
    date_to: Optional[datetime] = Query(None, description="Filter containers received on or before this date"),
    db: Session = Depends(get_db),
):
    """
    Paginated list of all shipping containers with filters and summary counts.

    **Filters:**
    - `received=true` — only containers with a received date
    - `received=false` — only containers NOT yet received (in transit / pending)
    - `search` — partial container name search (case-insensitive)
    - `po_id` — containers linked to a specific PO (local UUID)
    - `sellercloud_po_id` — containers linked to a specific PO (SC integer ID)
    - `vendor_id` — containers linked to POs from a specific vendor
    - `sellercloud_warehouse_id` — containers stored in a specific warehouse (SC integer ID)

    Each result includes:
    - `is_received` — boolean derived from received_date
    - `total_items` — number of distinct SKUs in the container
    - `total_qty_in_container` — total units across all items
    - `total_qty_received` — total units received from SC
    - `unique_pos` — number of distinct POs in the container
    """
    query = db.query(models.ShippingContainer).options(joinedload(models.ShippingContainer.warehouse))

    # Received / not-received filter
    if received is True:
        query = query.filter(models.ShippingContainer.received_date.isnot(None))
    elif received is False:
        query = query.filter(models.ShippingContainer.received_date.is_(None))

    order_clauses = []

    # Container name or ID search
    if search:
        from sqlalchemy import or_, case, cast, String
        import re
        escaped_search = re.escape(search)
        search_conditions = [
            models.ShippingContainer.container_name.op('~*')(rf"\y{escaped_search}")
        ]
        if search.isdigit():
            search_conditions.append(cast(models.ShippingContainer.sellercloud_container_id, String).op('~*')(rf"\y{escaped_search}"))
            order_clauses.append(case((models.ShippingContainer.sellercloud_container_id == int(search), 0), else_=1))
            
        query = query.filter(or_(*search_conditions))

    # Filter by Warehouse
    if sellercloud_warehouse_id:
        query = query.join(models.Warehouse, models.ShippingContainer.warehouse_id == models.Warehouse.id).filter(
            models.Warehouse.sellercloud_warehouse_id == sellercloud_warehouse_id
        )

    # Filter by PO (UUID or SC integer ID)
    if po_id or sellercloud_po_id or vendor_id:
        po_query = db.query(models.PurchaseOrderItemContainer.shipping_container_id) \
            .join(models.PurchaseOrderItem, models.PurchaseOrderItemContainer.purchase_order_item_id == models.PurchaseOrderItem.id) \
            .join(models.PurchaseOrder, models.PurchaseOrderItem.purchase_order_id == models.PurchaseOrder.id)
            
        if po_id:
            po_query = po_query.filter(models.PurchaseOrder.id == po_id)
        if sellercloud_po_id:
            po_query = po_query.filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id)
        if vendor_id:
            po_query = po_query.filter(models.PurchaseOrder.vendor_id == vendor_id)
            
        query = query.filter(models.ShippingContainer.id.in_(po_query))

    # Filter by received date range
    if date_from:
        query = query.filter(models.ShippingContainer.received_date >= date_from)
    if date_to:
        # If date_to has no time component (midnight), extend it to the end of the day
        if date_to.time() == datetime.min.time():
            from datetime import time
            date_to = datetime.combine(date_to.date(), time(23, 59, 59, 999999))
        query = query.filter(models.ShippingContainer.received_date <= date_to)

    total = query.count()
    containers = (
        query.order_by(*order_clauses, models.ShippingContainer.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # Build enriched results with summary counts
    results = []
    for ctr in containers:
        links = ctr.item_links  # loaded via relationship
        total_items = len(links)
        total_qty = sum(lnk.qty_in_container or 0 for lnk in links)
        total_received = total_qty if ctr.received_date else 0
        unique_po_ids = set(
            lnk.item.purchase_order_id for lnk in links if lnk.item
        )

        results.append(
            ContainerOut(
                id=ctr.id,
                sellercloud_container_id=ctr.sellercloud_container_id,
                container_name=ctr.container_name,
                estimated_arrival_date=ctr.estimated_arrival_date,
                received_date=ctr.received_date,
                is_received=ctr.received_date is not None,
                warehouse_id=ctr.warehouse_id,
                warehouse=ctr.warehouse,
                created_at=ctr.created_at,
                updated_at=ctr.updated_at,
                date_dropped_off=ctr.date_dropped_off,
                door=ctr.door,
                date_emptied=ctr.date_emptied,
                unloaded_by=ctr.unloaded_by,
                unload_cost=float(ctr.unload_cost) if ctr.unload_cost is not None else None,
                container_cost_drayage=float(ctr.container_cost_drayage) if ctr.container_cost_drayage is not None else None,
                customs_duty_misc=float(ctr.customs_duty_misc) if ctr.customs_duty_misc is not None else None,
                per_diem=float(ctr.per_diem) if ctr.per_diem is not None else None,
                country_of_origin=ctr.country_of_origin,
                receiving_closure_notes=ctr.receiving_closure_notes,
                factory_credit_needed=ctr.factory_credit_needed,
                total_items=total_items,
                total_qty_in_container=total_qty,
                total_qty_received=total_received,
                unique_pos=len(unique_po_ids),
            )
        )

    total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
    return ContainerListResponse(
        total=total,
        page=page,
        page_size=page_size,
        meta={
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "has_next": page * page_size < total,
            "has_prev": page > 1,
        },
        results=results,
    )


# ---------------------------------------------------------------------------
# GET /containers/po-items/{sellercloud_po_id}
# Returns PO line-items in a shape ready for the "create container" form.
# ---------------------------------------------------------------------------
@router.get("/po-items/{sellercloud_po_id}", response_model=POItemsForContainerResponse)
def get_po_items_for_container(
    sellercloud_po_id: int,
    db: Session = Depends(get_db),
):
    """
    Get all items from a PO formatted for the container-creation workflow.

    **For each line item returns:**
    - `po_item_id` — UUID to use in `POST /containers`
    - `sellercloud_item_id`, `sku`, `product_name`
    - `qty_ordered`, `qty_received`, `qty_remaining`
    - `qty_already_in_containers` — total already assigned to any container
    - `qty_available_for_container` — how many can still be added (ordered − already)
    - `existing_containers` — containers this item is already part of

    **Typical usage:**
    1. `GET /api/v1/containers/po-items/12345` — get item list
    2. User picks qty per item
    3. `POST /api/v1/containers` with the filled payload
    """
    po = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.items)
            .joinedload(models.PurchaseOrderItem.container_links)
            .joinedload(models.PurchaseOrderItemContainer.container),
        )
        .filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id)
        .first()
    )

    if not po:
        raise HTTPException(
            status_code=404, detail=f"Purchase Order {sellercloud_po_id} not found"
        )

    from app.schemas import POItemForContainerOut, ContainerSummary as CS

    items_out = []
    for item in po.items:
        qty_already = sum(link.qty_in_container or 0 for link in item.container_links)
        qty_available = max(0, item.qty_ordered - qty_already)

        existing_containers = [
            CS(
                id=link.container.id,
                sellercloud_container_id=link.container.sellercloud_container_id,
                container_name=link.container.container_name,
                estimated_arrival_date=link.container.estimated_arrival_date,
                received_date=link.container.received_date,
                qty_in_container=link.qty_in_container,
            )
            for link in item.container_links
            if link.container
        ]

        items_out.append(
            POItemForContainerOut(
                po_item_id=item.id,
                sellercloud_item_id=item.sellercloud_item_id,
                sellercloud_po_id=po.sellercloud_po_id,
                sku=item.sku,
                product_name=item.product_name,
                qty_ordered=item.qty_ordered,
                qty_received=item.qty_received,
                qty_remaining=qty_available,
                qty_already_in_containers=qty_already,
                qty_available_for_container=qty_available,
                existing_containers=existing_containers,
            )
        )

    total_ordered = sum(i.qty_ordered for i in items_out)
    total_received = sum(i.qty_received for i in items_out)
    total_in_containers = sum(i.qty_already_in_containers for i in items_out)
    total_available = sum(i.qty_available_for_container for i in items_out)

    return POItemsForContainerResponse(
        po_id=po.id,
        sellercloud_po_id=po.sellercloud_po_id,
        po_title=po.purchase_title,
        vendor_name=po.vendor.name if po.vendor else None,
        items=items_out,
        summary={
            "total_items": len(items_out),
            "total_qty_ordered": total_ordered,
            "total_qty_received": total_received,
            "total_qty_in_containers": total_in_containers,
            "total_qty_available_for_container": total_available,
        },
    )


# ---------------------------------------------------------------------------
# POST /containers  (and legacy /containers/create)
# Creates a container locally + syncs to SellerCloud
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
@router.post("/create", status_code=201, include_in_schema=False)  # backward compat
def create_container(
    container_data: ContainerCreate,
    db: Session = Depends(get_db),
):
    """
    Create a new shipping container, sync it to SellerCloud, and link PO items.

    **Workflow:**
    1. `GET /api/v1/containers/po-items/{sellercloud_po_id}` — fetch items + available qty
    2. Build the request body with the items you want in this container
    3. POST here

    **Request body example:**
    ```json
    {
      "container_name": "CONT-2026-001",
      "estimated_arrival_date": "2026-09-01T00:00:00Z",
      "items": [
        { "po_item_id": "<uuid>", "qty_in_container": 50 },
        { "po_item_id": "<uuid>", "qty_in_container": 30 }
      ]
    }
    ```

    **Item resolution order** (use whichever identifiers you have):
    1. `po_item_id` — local UUID (preferred, from po-items endpoint)
    2. `sellercloud_item_id` — SellerCloud PO line item ID
    3. `sellercloud_po_id` + `sku` — PO ID + product SKU

    **What this does:**
    - Validates items exist and qty doesn't exceed what's available
    - Creates container in SellerCloud via `POST /api/ShippingContainers`
    - Saves container + item links in local DB
    - Updates `qty_in_container` on each PO item
    - Returns created container with SC ID (if sync succeeded)
    """
    resolved_items = []

    for item_data in container_data.items:
        item = None

        if item_data.po_item_id:
            item = (
                db.query(models.PurchaseOrderItem)
                .filter(models.PurchaseOrderItem.id == item_data.po_item_id)
                .options(joinedload(models.PurchaseOrderItem.purchase_order))
                .first()
            )
        elif item_data.sellercloud_item_id:
            item = (
                db.query(models.PurchaseOrderItem)
                .filter(
                    models.PurchaseOrderItem.sellercloud_item_id
                    == item_data.sellercloud_item_id
                )
                .options(joinedload(models.PurchaseOrderItem.purchase_order))
                .first()
            )
        elif item_data.sellercloud_po_id and item_data.sku:
            item = (
                db.query(models.PurchaseOrderItem)
                .join(models.PurchaseOrder)
                .filter(
                    models.PurchaseOrder.sellercloud_po_id == item_data.sellercloud_po_id,
                    models.PurchaseOrderItem.sku == item_data.sku,
                )
                .options(joinedload(models.PurchaseOrderItem.purchase_order))
                .first()
            )

        if not item:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"PO item not found — tried: "
                    f"po_item_id={item_data.po_item_id}, "
                    f"sellercloud_item_id={item_data.sellercloud_item_id}, "
                    f"sellercloud_po_id={item_data.sellercloud_po_id}, "
                    f"sku={item_data.sku}"
                ),
            )

        # Guard: don't allow over-assignment beyond qty_ordered
        qty_rows = (
            db.query(models.PurchaseOrderItemContainer.qty_in_container)
            .filter(
                models.PurchaseOrderItemContainer.purchase_order_item_id == item.id
            )
            .all()
        )
        qty_already_total = sum(r[0] or 0 for r in qty_rows)
        qty_available = max(0, item.qty_ordered - qty_already_total)

        if item_data.qty_in_container > qty_available:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"SKU '{item.sku}': requested {item_data.qty_in_container} "
                    f"but only {qty_available} available "
                    f"(ordered={item.qty_ordered}, already_in_containers={qty_already_total})"
                ),
            )

        resolved_items.append((item_data, item))

    # Build SellerCloud create payload — SC only accepts name/dates at creation time
    # Items are added in a SECOND call after the container is created
    sc_container_payload = {
        "ContainerName": container_data.container_name,
        "EstimatedArrivalDate": format_sc_date(container_data.estimated_arrival_date),
        "ShippingStatus": 2 if container_data.received_date else 1,
    }
    if container_data.received_date:
        sc_container_payload["ReceivedDate"] = format_sc_date(container_data.received_date)
        
    if container_data.warehouse_id:
        import uuid
        if isinstance(container_data.warehouse_id, uuid.UUID) or (isinstance(container_data.warehouse_id, str) and '-' in str(container_data.warehouse_id)):
            warehouse = db.query(models.Warehouse).filter(models.Warehouse.id == str(container_data.warehouse_id)).first()
        else:
            warehouse = db.query(models.Warehouse).filter(models.Warehouse.sellercloud_warehouse_id == int(container_data.warehouse_id)).first()
            
        if warehouse:
            if warehouse.sellercloud_warehouse_id:
                sc_container_payload["ReceiveWarehouseID"] = warehouse.sellercloud_warehouse_id
            # Reassign container_data.warehouse_id to the UUID for local saving
            container_data.warehouse_id = warehouse.id

    sc_response: dict = {}
    sellercloud_container_id = None
    sc_sync_error = None

    try:
        sc_client = SellerCloudClient()

        # STEP 1: Create the container (SC API only accepts name/dates — NOT items)
        sc_response = sc_client.create_shipping_container(sc_container_payload)
        if isinstance(sc_response, dict):
            sellercloud_container_id = (
                sc_response.get("ID")
                or sc_response.get("Id")
                or sc_response.get("ContainerID")
                or sc_response.get("ContainerId")
                or sc_response.get("id")
            )

        # Fallback: SC sometimes returns empty body — search by name to recover ID
        if not sellercloud_container_id:
            try:
                search_resp = sc_client.search_containers_by_name(container_data.container_name)
                matches = search_resp.get("Items") or []
                for m in matches:
                    if m.get("ContainerName") == container_data.container_name:
                        sellercloud_container_id = m.get("ID") or m.get("Id")
                        if sellercloud_container_id:
                            sc_response["_recovered_id"] = sellercloud_container_id
                            break
            except Exception as lookup_err:
                print(f"[create_container] ID recovery search failed: {lookup_err}")

        # STEP 2: Add items to the container (separate SC endpoint)
        if sellercloud_container_id:
            try:
                sc_items = [
                    {
                        "PurchaseOrderID": (
                            item.purchase_order.sellercloud_po_id
                            if item.purchase_order and item.purchase_order.sellercloud_po_id
                            else item_data.sellercloud_po_id
                        ),
                        "PurchaseOrderItemID": item.sellercloud_item_id or item_data.sellercloud_item_id,
                        "Qty": item_data.qty_in_container,
                    }
                    for item_data, item in resolved_items
                ]
                sc_client.add_items_to_container(sellercloud_container_id, sc_items)
                print(f"[create_container] Added {len(sc_items)} items to SC container {sellercloud_container_id}")
            except Exception as items_err:
                print(f"[create_container] SC add-items failed (container created, items not linked in SC): {items_err}")
                sc_response["_items_error"] = str(items_err)

    except Exception as exc:
        sc_sync_error = str(exc)
        print(f"[create_container] SellerCloud sync failed — saving locally anyway: {exc}")

    # Persist locally regardless of SC outcome
    new_container = models.ShippingContainer(
        container_name=container_data.container_name,
        estimated_arrival_date=container_data.estimated_arrival_date,
        received_date=container_data.received_date,
        sellercloud_container_id=sellercloud_container_id,
        warehouse_id=container_data.warehouse_id,
        date_dropped_off=container_data.date_dropped_off,
        door=container_data.door,
        date_emptied=container_data.date_emptied,
        unloaded_by=container_data.unloaded_by,
        unload_cost=container_data.unload_cost,
        container_cost_drayage=container_data.container_cost_drayage,
        customs_duty_misc=container_data.customs_duty_misc,
        per_diem=container_data.per_diem,
        country_of_origin=container_data.country_of_origin,
        receiving_closure_notes=container_data.receiving_closure_notes,
        factory_credit_needed=container_data.factory_credit_needed,
        raw_json=sc_response if isinstance(sc_response, dict) else {"error": sc_sync_error},
    )
    db.add(new_container)
    db.flush()

    linked_items_summary = []
    for item_data, item in resolved_items:
        db.add(
            models.PurchaseOrderItemContainer(
                purchase_order_item_id=item.id,
                shipping_container_id=new_container.id,
                qty_in_container=item_data.qty_in_container,
                raw_json={"sc_payload_item": item_data.model_dump(mode="json")},
            )
        )
        item.qty_in_container = (item.qty_in_container or 0) + item_data.qty_in_container

        linked_items_summary.append({
            "po_item_id": str(item.id),
            "sellercloud_item_id": item.sellercloud_item_id,
            "sellercloud_po_id": (
                item.purchase_order.sellercloud_po_id if item.purchase_order else None
            ),
            "sku": item.sku,
            "product_name": item.product_name,
            "qty_in_container": item_data.qty_in_container,
            "total_item_qty_in_containers": item.qty_in_container,
        })

    db.commit()
    db.refresh(new_container)

    response = {
        "success": True,
        "sellercloud_synced": sellercloud_container_id is not None,
        "message": (
            "Container created and synced to SellerCloud"
            if sellercloud_container_id
            else "Container saved locally — SellerCloud sync failed (see sc_sync_error)"
        ),
        "container": {
            "id": str(new_container.id),
            "sellercloud_container_id": new_container.sellercloud_container_id,
            "container_name": new_container.container_name,
            "estimated_arrival_date": (
                new_container.estimated_arrival_date.isoformat()
                if new_container.estimated_arrival_date
                else None
            ),
            "received_date": (
                new_container.received_date.isoformat()
                if new_container.received_date
                else None
            ),
            "items_linked": len(linked_items_summary),
            "items": linked_items_summary,
        },
    }
    if sc_sync_error:
        response["sc_sync_error"] = sc_sync_error

    return response


# ---------------------------------------------------------------------------
# PUT /containers/{container_id}
# ---------------------------------------------------------------------------
@router.put("/{container_id}")
def update_container(
    container_id: str,
    update_data: ContainerUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Update a container's name, estimated arrival date, or received date.
    Syncs the update to SellerCloud and saves it locally.
    """
    container = db.query(models.ShippingContainer).filter(resolve_container_filter(container_id)).first()
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")

    # If it's synced to SellerCloud, push the update
    if container.sellercloud_container_id:
        try:
            sc_client = SellerCloudClient()
            final_received_date = update_data.received_date if update_data.received_date is not None else container.received_date
            sc_payload = {
                "ContainerName": update_data.container_name or container.container_name,
                "EstimatedArrivalDate": format_sc_date(update_data.estimated_arrival_date if update_data.estimated_arrival_date else container.estimated_arrival_date),
                "ReceivedDate": format_sc_date(final_received_date),
                "ShippingStatus": 2 if final_received_date else 1,
            }
            sc_client.update_shipping_container(container.sellercloud_container_id, sc_payload)
        except Exception as exc:
            print(f"Failed to update container {container.sellercloud_container_id} in SellerCloud: {exc}")
            # We can optionally fail here or continue saving locally
            # raise HTTPException(status_code=500, detail=f"SellerCloud sync failed: {exc}")

    # Update local DB
    if update_data.container_name is not None:
        container.container_name = update_data.container_name
    if update_data.estimated_arrival_date is not None:
        container.estimated_arrival_date = update_data.estimated_arrival_date
    if update_data.received_date is not None:
        container.received_date = update_data.received_date

    # Update lifecycle fields dynamically if they are passed in the request
    update_dict = update_data.model_dump(exclude_unset=True)
    lifecycle_fields = [
        "date_dropped_off", "door", "date_emptied", "unloaded_by", 
        "unload_cost", "container_cost_drayage", "customs_duty_misc", 
        "per_diem", "country_of_origin", "receiving_closure_notes", 
        "factory_credit_needed"
    ]
    for field in lifecycle_fields:
        if field in update_dict:
            setattr(container, field, update_dict[field])

    container.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(container)

    log_activity(db, action="UPDATE_CONTAINER", user_id=current_user.id, entity_type="CONTAINER", entity_id=str(container.id), details=update_data.model_dump(mode='json', exclude_unset=True))

    return {
        "success": True,
        "message": "Container updated successfully",
        "container": {
            "id": str(container.id),
            "container_name": container.container_name,
            "estimated_arrival_date": container.estimated_arrival_date,
            "received_date": container.received_date,
            "sellercloud_container_id": container.sellercloud_container_id
        }
    }


# ---------------------------------------------------------------------------
# POST /containers/{container_id}/items
# ---------------------------------------------------------------------------
@router.post("/{container_id}/items")
def add_items_to_container(
    container_id: str,
    items_data: ContainerAddItems,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Add items to an existing container. Syncs to SellerCloud and saves locally.
    """
    container = db.query(models.ShippingContainer).filter(resolve_container_filter(container_id)).first()
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")

    resolved_items = []
    
    # 1. Resolve and validate items exactly like create_container
    for item_data in items_data.items:
        item = None
        if item_data.po_item_id:
            item = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.id == item_data.po_item_id).options(joinedload(models.PurchaseOrderItem.purchase_order)).first()
        elif item_data.sellercloud_item_id:
            item = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.sellercloud_item_id == item_data.sellercloud_item_id).options(joinedload(models.PurchaseOrderItem.purchase_order)).first()
        elif item_data.sellercloud_po_id and item_data.sku:
            item = db.query(models.PurchaseOrderItem).join(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == item_data.sellercloud_po_id, models.PurchaseOrderItem.sku == item_data.sku).options(joinedload(models.PurchaseOrderItem.purchase_order)).first()

        if not item:
            raise HTTPException(status_code=404, detail=f"PO item not found for data: {item_data}")

        # Check availability
        qty_rows = db.query(models.PurchaseOrderItemContainer.qty_in_container).filter(models.PurchaseOrderItemContainer.purchase_order_item_id == item.id).all()
        qty_already_total = sum(r[0] or 0 for r in qty_rows)
        qty_available = max(0, item.qty_ordered - qty_already_total)

        if item_data.qty_in_container > qty_available:
            raise HTTPException(status_code=400, detail=f"SKU '{item.sku}': requested {item_data.qty_in_container} but only {qty_available} available")

        # Check if already in this container
        existing_link = db.query(models.PurchaseOrderItemContainer).filter(models.PurchaseOrderItemContainer.purchase_order_item_id == item.id, models.PurchaseOrderItemContainer.shipping_container_id == container.id).first()
        if existing_link:
            raise HTTPException(status_code=400, detail=f"SKU '{item.sku}' is already in this container. Update functionality is not supported by this endpoint.")

        resolved_items.append((item_data, item))

    # 2. Sync to SellerCloud
    if container.sellercloud_container_id:
        try:
            sc_client = SellerCloudClient()
            sc_items = [
                {
                    "PurchaseOrderID": (item.purchase_order.sellercloud_po_id if item.purchase_order and item.purchase_order.sellercloud_po_id else item_data.sellercloud_po_id),
                    "PurchaseOrderItemID": item.sellercloud_item_id or item_data.sellercloud_item_id,
                    "Qty": item_data.qty_in_container,
                }
                for item_data, item in resolved_items
            ]
            sc_client.add_items_to_container(container.sellercloud_container_id, sc_items)
        except Exception as exc:
            print(f"Failed to add items to SC container {container.sellercloud_container_id}: {exc}")
            raise HTTPException(status_code=500, detail=f"SellerCloud sync failed: {exc}")

    # 3. Update local DB
    linked_items_summary = []
    for item_data, item in resolved_items:
        db.add(
            models.PurchaseOrderItemContainer(
                purchase_order_item_id=item.id,
                shipping_container_id=container.id,
                qty_in_container=item_data.qty_in_container,
                raw_json={"sc_payload_item": item_data.model_dump(mode="json")}
            )
        )
        item.qty_in_container = (item.qty_in_container or 0) + item_data.qty_in_container
        
        linked_items_summary.append({
            "sku": item.sku,
            "qty_added": item_data.qty_in_container,
            "total_item_qty_in_containers": item.qty_in_container
        })

    container.updated_at = datetime.utcnow()
    db.commit()

    # Recalculate PO shipment status for affected POs
    from app.services.po_service import recalculate_po_shipment_status
    affected_po_ids = set(item.purchase_order_id for _, item in resolved_items if item.purchase_order_id)
    for po_id in affected_po_ids:
        recalculate_po_shipment_status(db, str(po_id))

    log_activity(db, action="ADD_ITEMS_TO_CONTAINER", user_id=current_user.id, entity_type="CONTAINER", entity_id=str(container.id), details={"items_added": len(resolved_items)})

    return {
        "success": True,
        "message": f"Successfully added {len(resolved_items)} items to container",
        "items_added": linked_items_summary
    }


# ---------------------------------------------------------------------------
# GET /containers/{container_id}/details
# ---------------------------------------------------------------------------
@router.get("/{container_id}/details", response_model=ContainerDetailOut)
def get_container_details(
    container_id: str,
    db: Session = Depends(get_db),
):
    """
    Full detail for one container.

    Returns:
    - Container info: name, ETA, received date, `is_received` flag
    - Summary: total qty, item count, unique POs, received qty
    - Items: every SKU in the container with full PO context and received status

    Each item includes `is_fully_received` (True when qty_received >= qty_ordered).
    """
    container = (
        db.query(models.ShippingContainer)
        .filter(resolve_container_filter(container_id))
        .options(
            joinedload(models.ShippingContainer.item_links)
            .joinedload(models.PurchaseOrderItemContainer.item)
            .joinedload(models.PurchaseOrderItem.purchase_order)
            .joinedload(models.PurchaseOrder.vendor)
        )
        .first()
    )
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")

    items_out = []
    for link in container.item_links:
        item = link.item
        po = item.purchase_order if item else None
        items_out.append(
            ContainerDetailItemOut(
                po_item_id=item.id,
                sellercloud_item_id=item.sellercloud_item_id,
                sellercloud_po_id=po.sellercloud_po_id if po else None,
                po_title=po.purchase_title if po else None,
                vendor_name=po.vendor.name if (po and po.vendor) else None,
                sku=item.sku,
                product_name=item.product_name,
                qty_in_container=link.qty_in_container or 0,
                qty_ordered=item.qty_ordered,
                qty_received=item.qty_received,
                qty_remaining=max(0, item.qty_ordered - item.qty_received),
                is_fully_received=item.qty_received >= item.qty_ordered,
                unit_price=float(item.unit_price) if item.unit_price else None,
            )
        )

    total_qty = sum(i.qty_in_container for i in items_out)
    total_received_qty = total_qty if container.received_date else 0
    unique_po_ids = set(
        str(link.item.purchase_order_id)
        for link in container.item_links
        if link.item
    )
    fully_received_count = sum(1 for i in items_out if i.is_fully_received)

    return ContainerDetailOut(
        id=container.id,
        sellercloud_container_id=container.sellercloud_container_id,
        container_name=container.container_name,
        estimated_arrival_date=container.estimated_arrival_date,
        received_date=container.received_date,
        is_received=container.received_date is not None,
        created_at=container.created_at,
        updated_at=container.updated_at,
        date_dropped_off=container.date_dropped_off,
        door=container.door,
        date_emptied=container.date_emptied,
        unloaded_by=container.unloaded_by,
        unload_cost=float(container.unload_cost) if container.unload_cost is not None else None,
        container_cost_drayage=float(container.container_cost_drayage) if container.container_cost_drayage is not None else None,
        customs_duty_misc=float(container.customs_duty_misc) if container.customs_duty_misc is not None else None,
        per_diem=float(container.per_diem) if container.per_diem is not None else None,
        country_of_origin=container.country_of_origin,
        receiving_closure_notes=container.receiving_closure_notes,
        factory_credit_needed=container.factory_credit_needed,
        summary={
            "total_items": len(items_out),
            "total_qty_in_container": total_qty,
            "total_qty_received": total_received_qty,
            "unique_purchase_orders": len(unique_po_ids),
            "fully_received_items": fully_received_count,
            "pending_items": len(items_out) - fully_received_count,
        },
        items=items_out,
    )


# ---------------------------------------------------------------------------
# POST /containers/{container_id}/sync
# Re-pull container info from SellerCloud
# ---------------------------------------------------------------------------
import httpx

@router.post("/{container_id}/sync")
def sync_container_from_sellercloud(
    container_id: str,
    db: Session = Depends(get_db),
):
    """
    Re-sync a container from SellerCloud (name, ETA, received date).
    Useful after changes are made directly in SellerCloud.
    """
    container = (
        db.query(models.ShippingContainer)
        .filter(resolve_container_filter(container_id))
        .first()
    )
    if not container:
        raise HTTPException(status_code=404, detail="Container not found")

    if not container.sellercloud_container_id:
        raise HTTPException(
            status_code=400,
            detail="Container has no SellerCloud ID — cannot sync",
        )

    try:
        sc_client = SellerCloudClient()
        sc_resp = sc_client.get(f"/api/ShippingContainers/{container.sellercloud_container_id}")
        
        from app.services.sync_service import _get_or_create_warehouse

        # SC response: { "Details": {ContainerName, EstimatedArrivalDate, ReceivedOnDate}, "Items": {...} }
        sc = sc_resp.get("Details") or sc_resp

        container.container_name = sc.get("ContainerName") or container.container_name

        if sc.get("EstimatedArrivalDate"):
            container.estimated_arrival_date = datetime.fromisoformat(
                sc["EstimatedArrivalDate"].replace("Z", "+00:00")
            )

        received_raw = sc.get("ReceivedOnDate") or sc.get("ReceivedDate")
        if received_raw:
            container.received_date = datetime.fromisoformat(
                received_raw.replace("Z", "+00:00")
            )

        warehouse_sc_id = sc.get("ReceivingWarehouseID") or sc.get("ReceiveWarehouseID")
        warehouse = _get_or_create_warehouse(db, warehouse_sc_id)
        if warehouse:
            container.warehouse_id = warehouse.id

        container.updated_at = datetime.utcnow()
        db.commit()

        return {
            "success": True,
            "message": "Container synced from SellerCloud",
            "container": {
                "id": str(container.id),
                "sellercloud_container_id": container.sellercloud_container_id,
                "container_name": container.container_name,
                "estimated_arrival_date": (
                    container.estimated_arrival_date.isoformat()
                    if container.estimated_arrival_date
                    else None
                ),
                "received_date": (
                    container.received_date.isoformat() if container.received_date else None
                ),
                "warehouse_id": str(container.warehouse_id) if container.warehouse_id else None,
                "warehouse": {
                    "id": str(warehouse.id),
                    "sellercloud_warehouse_id": warehouse.sellercloud_warehouse_id,
                    "name": warehouse.name,
                    "is_default": warehouse.is_default,
                    "warehouse_type": warehouse.warehouse_type,
                    "is_sellable": warehouse.is_sellable
                } if warehouse else None
            },
        }

    except httpx.HTTPStatusError as exc:
        db.rollback()
        if exc.response.status_code == 404:
            # The container was deleted in SellerCloud, so delete it locally
            db.delete(container)
            db.commit()
            return {
                "success": True,
                "message": "Container was deleted in SellerCloud and has been removed locally.",
                "deleted": True
            }
        return {
            "success": False,
            "message": "Error syncing from SellerCloud",
            "error": str(exc)
        }
    except Exception as exc:
        db.rollback()
        # Return structured error response instead of 500 so frontend can handle it
        return {
            "success": False,
            "message": "Error syncing from SellerCloud",
            "error": str(exc)
        }


# ---------------------------------------------------------------------------
# GET /containers/summary/counts
# Quick dashboard counts
# ---------------------------------------------------------------------------
@router.get("/summary/counts")
def get_container_counts(db: Session = Depends(get_db)):
    """
    Quick summary counts for dashboard use.

    Returns:
    - `total` — all containers
    - `received` — containers with received_date set
    - `pending` — containers with no received_date (in transit / not arrived)
    - `synced_to_sc` — containers with a SellerCloud ID
    - `local_only` — containers created locally but SC sync failed (no SC ID)
    """
    total = db.query(models.ShippingContainer).count()
    received = db.query(models.ShippingContainer).filter(
        models.ShippingContainer.received_date.isnot(None)
    ).count()
    synced = db.query(models.ShippingContainer).filter(
        models.ShippingContainer.sellercloud_container_id.isnot(None)
    ).count()

    return {
        "total": total,
        "received": received,
        "pending": total - received,
        "synced_to_sc": synced,
        "local_only": total - synced,
    }


# ---------------------------------------------------------------------------
# GET /containers/by-po/{sellercloud_po_id}
# ---------------------------------------------------------------------------
@router.get("/by-po/{sellercloud_po_id}")
def get_containers_for_po(
    sellercloud_po_id: int,
    db: Session = Depends(get_db),
):
    """
    All containers associated with a specific PO (by SellerCloud PO ID).
    Returns each container grouped with the items from this PO only.
    """
    po = (
        db.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id)
        .first()
    )
    if not po:
        raise HTTPException(
            status_code=404, detail=f"Purchase Order {sellercloud_po_id} not found"
        )

    container_links = (
        db.query(models.PurchaseOrderItemContainer)
        .join(
            models.PurchaseOrderItem,
            models.PurchaseOrderItemContainer.purchase_order_item_id
            == models.PurchaseOrderItem.id,
        )
        .filter(models.PurchaseOrderItem.purchase_order_id == po.id)
        .options(
            joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrderItemContainer.item),
        )
        .all()
    )

    containers_dict: dict = {}
    for link in container_links:
        ctr = link.container
        item = link.item
        if ctr.id not in containers_dict:
            containers_dict[ctr.id] = {
                "container": {
                    "id": str(ctr.id),
                    "sellercloud_container_id": ctr.sellercloud_container_id,
                    "container_name": ctr.container_name,
                    "estimated_arrival_date": (
                        ctr.estimated_arrival_date.isoformat()
                        if ctr.estimated_arrival_date
                        else None
                    ),
                    "received_date": (
                        ctr.received_date.isoformat() if ctr.received_date else None
                    ),
                },
                "items": [],
            }
        containers_dict[ctr.id]["items"].append({
            "item_id": str(item.id),
            "sku": item.sku,
            "product_name": item.product_name,
            "qty_in_container": link.qty_in_container,
            "qty_ordered": item.qty_ordered,
            "qty_received": item.qty_received,
            "qty_remaining": item.qty_ordered - item.qty_received,
        })

    containers_list = []
    for cdata in containers_dict.values():
        cdata["total_qty"] = sum(i["qty_in_container"] for i in cdata["items"])
        cdata["items_count"] = len(cdata["items"])
        containers_list.append(cdata)

    return {
        "po_id": str(po.id),
        "sellercloud_po_id": po.sellercloud_po_id,
        "po_title": po.purchase_title,
        "containers_count": len(containers_list),
        "containers": containers_list,
    }


# ---------------------------------------------------------------------------
# POST /containers/debug-sc-create
# Fires the exact SellerCloud payload WITHOUT saving anything locally.
# Use this once to confirm what SC returns so you know the correct ID field.
# ---------------------------------------------------------------------------
@router.post("/debug-sc-create")
def debug_sc_create(
    container_data: ContainerCreate,
    db: Session = Depends(get_db),
):
    """
    **DEBUG ONLY** — sends the container payload to SellerCloud and returns the
    raw response without saving anything to the local database.

    Use this once to answer: *what does SC actually return when a container is
    created?* — specifically which field holds the new container integer ID.

    Once confirmed, the main `POST /containers` endpoint handles it correctly.

    The response includes:
    - `sc_payload_sent` — exactly what we sent to SellerCloud
    - `sc_raw_response` — raw response body as a string
    - `sc_parsed_response` — parsed JSON (or error)
    - `sc_status_code` — HTTP status SC returned
    - `detected_id` — the ID we would extract (if any)
    """
    # Resolve items to build the SC payload (same logic as create, no DB writes)
    sc_items_payload = []
    for item_data in container_data.items:
        item = None
        if item_data.po_item_id:
            item = (
                db.query(models.PurchaseOrderItem)
                .filter(models.PurchaseOrderItem.id == item_data.po_item_id)
                .options(joinedload(models.PurchaseOrderItem.purchase_order))
                .first()
            )
        elif item_data.sellercloud_item_id:
            item = (
                db.query(models.PurchaseOrderItem)
                .filter(models.PurchaseOrderItem.sellercloud_item_id == item_data.sellercloud_item_id)
                .options(joinedload(models.PurchaseOrderItem.purchase_order))
                .first()
            )
        elif item_data.sellercloud_po_id and item_data.sku:
            item = (
                db.query(models.PurchaseOrderItem)
                .join(models.PurchaseOrder)
                .filter(
                    models.PurchaseOrder.sellercloud_po_id == item_data.sellercloud_po_id,
                    models.PurchaseOrderItem.sku == item_data.sku,
                )
                .options(joinedload(models.PurchaseOrderItem.purchase_order))
                .first()
            )

        if not item:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_data}")

        sc_items_payload.append({
            "PurchaseOrderID": item.purchase_order.sellercloud_po_id if item.purchase_order else item_data.sellercloud_po_id,
            "PurchaseOrderItemID": item.sellercloud_item_id or item_data.sellercloud_item_id,
            "Qty": item_data.qty_in_container,
        })

    # Per SC Swagger: POST /api/ShippingContainers only accepts name/dates
    # Items go to POST /api/ShippingContainers/{id}/Items separately
    sc_payload = {
        "ContainerName": container_data.container_name,
        "EstimatedArrivalDate": container_data.estimated_arrival_date.isoformat() if container_data.estimated_arrival_date else None,
        "ShippingStatus": 2 if container_data.received_date else 1,
    }
    if container_data.received_date:
        sc_payload["ReceivedDate"] = container_data.received_date.isoformat()

    # Build the add-items payload for display (not called since this is debug-only)
    sc_add_items_payload = {"Items": sc_items_payload}

    sc_raw = None
    sc_parsed = None
    sc_status = None
    sc_items_raw = None
    sc_items_status = None
    error = None

    try:
        sc_client = SellerCloudClient()
        # STEP 1: Test the container create call
        resp = sc_client._request("POST", "/api/ShippingContainers", json=sc_payload)
        sc_status = resp.status_code
        sc_raw = resp.text
        try:
            sc_parsed = resp.json()
        except Exception:
            sc_parsed = {"_not_json": sc_raw}

        # Extract container ID from the create response
        detected_id = None
        if isinstance(sc_parsed, dict):
            detected_id = (
                sc_parsed.get("ID") or sc_parsed.get("Id") or
                sc_parsed.get("ContainerID") or sc_parsed.get("ContainerId") or
                sc_parsed.get("id")
            )
        elif isinstance(sc_parsed, int):
            detected_id = sc_parsed
        # Also handle plain integer body
        if not detected_id and sc_raw and sc_raw.strip().isdigit():
            detected_id = int(sc_raw.strip())

        # STEP 2: If we got an ID, test the add-items call too
        if detected_id and sc_items_payload:
            try:
                items_resp = sc_client._request(
                    "POST",
                    f"/api/ShippingContainers/{detected_id}/Items",
                    json={"Items": sc_items_payload},
                )
                sc_items_status = items_resp.status_code
                sc_items_raw = items_resp.text
            except Exception as items_exc:
                sc_items_raw = f"ERROR: {items_exc}"

    except Exception as exc:
        error = str(exc)
        detected_id = None

    return {
        "note": "DEBUG ONLY — this DID create a container + add items in SellerCloud. Delete it from SC if testing.",
        "step1_create_container": {
            "sc_status_code": sc_status,
            "sc_payload_sent": sc_payload,
            "sc_raw_response": sc_raw,
            "sc_parsed_response": sc_parsed,
            "detected_id": detected_id,
        },
        "step2_add_items": {
            "sc_status_code": sc_items_status,
            "sc_payload_sent": sc_add_items_payload if detected_id else "SKIPPED — no container ID from step 1",
            "sc_raw_response": sc_items_raw,
        },
        "error": error,
    }

import csv
import io
from fastapi.responses import StreamingResponse
from app.schemas import ContainerExportRequest
from sqlalchemy.orm import joinedload

# ---------------------------------------------------------------------------
# POST /containers/export/csv
# Export containers to CSV
# ---------------------------------------------------------------------------
@router.post("/export/csv")
def export_containers_csv(
    request_data: ContainerExportRequest,
    db: Session = Depends(get_db)
):
    """
    Export containers to a CSV file.
    Creates a row for every item inside the matched containers.
    """
    query = db.query(models.ShippingContainer)
    
    if request_data.container_ids:
        query = query.filter(models.ShippingContainer.id.in_(request_data.container_ids))
        
    if request_data.is_received is not None:
        if request_data.is_received:
            query = query.filter(models.ShippingContainer.received_date.isnot(None))
        else:
            query = query.filter(models.ShippingContainer.received_date.is_(None))
            
    # Eager load relationships
    query = query.options(
        joinedload(models.ShippingContainer.warehouse),
        joinedload(models.ShippingContainer.item_links)
        .joinedload(models.PurchaseOrderItemContainer.item)
        .joinedload(models.PurchaseOrderItem.purchase_order)
    )
    
    containers = query.order_by(models.ShippingContainer.created_at.desc()).all()
    
    # Define available columns
    DEFAULT_COLUMNS = [
        "container_name",
        "sellercloud_container_id",
        "estimated_arrival_date",
        "received_date",
        "warehouse_name",
        "sellercloud_po_id",
        "po_order_number",
        "sku",
        "item_name",
        "qty_ordered",
        "qty_in_container"
    ]
    
    columns = request_data.columns if request_data.columns else DEFAULT_COLUMNS
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    
    item_specific_cols = {
        "sellercloud_po_id",
        "po_order_number",
        "sku",
        "item_name",
        "qty_ordered",
        "qty_in_container"
    }
    
    requires_items = any(col in item_specific_cols for col in columns)
    
    for ctr in containers:
        warehouse_name = ctr.warehouse.name if ctr.warehouse else ""
        
        if not ctr.item_links or not requires_items:
            # Container with no items or item-level columns not requested
            row_dict = {
                "container_name": ctr.container_name or "",
                "sellercloud_container_id": ctr.sellercloud_container_id or "",
                "estimated_arrival_date": ctr.estimated_arrival_date.strftime("%Y-%m-%d") if ctr.estimated_arrival_date else "",
                "received_date": ctr.received_date.strftime("%Y-%m-%d") if ctr.received_date else "",
                "warehouse_name": warehouse_name,
                "sellercloud_po_id": "",
                "po_order_number": "",
                "sku": "",
                "item_name": "",
                "qty_ordered": "",
                "qty_in_container": ""
            }
            writer.writerow([row_dict.get(col, "") for col in columns])
        else:
            for link in ctr.item_links:
                item = link.item
                po = item.purchase_order if item else None
                
                po_order_number = ""
                if po and po.purchase_title:
                    import re
                    match = re.search(r'#(\d+)', po.purchase_title)
                    if match:
                        po_order_number = match.group(1)

                row_dict = {
                    "container_name": ctr.container_name or "",
                    "sellercloud_container_id": ctr.sellercloud_container_id or "",
                    "estimated_arrival_date": ctr.estimated_arrival_date.strftime("%Y-%m-%d") if ctr.estimated_arrival_date else "",
                    "received_date": ctr.received_date.strftime("%Y-%m-%d") if ctr.received_date else "",
                    "warehouse_name": warehouse_name,
                    "sellercloud_po_id": po.sellercloud_po_id if po else "",
                    "po_order_number": po_order_number,
                    "sku": item.sku if item else "",
                    "item_name": item.product_name if item else "",
                    "qty_ordered": item.qty_ordered if item else "",
                    "qty_in_container": link.qty_in_container or 0
                }
                writer.writerow([row_dict.get(col, "") for col in columns])
                
    output.seek(0)
    from datetime import datetime
    filename = f"containers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ---------------------------------------------------------------------------
# POST /containers/import/preview
# Read an uploaded CSV or Excel file and return found PO items
# ---------------------------------------------------------------------------
@router.post("/import/preview")
async def preview_container_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    Reads an uploaded CSV or Excel file containing container items.
    Tries to map each row to a PurchaseOrderItem in the database.
    
    Expected columns (flexible naming):
    - POID / PO ID
    - ProductID / SKU / ProductID - SKU
    - Quantity / Qty
    
    Returns a JSON array where each row shows the data from the file
    plus the matched `po_item_id` and item details, or `null` if not found.
    """
    if not file.filename.endswith(('.csv', '.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Only CSV or Excel files are supported")
        
    try:
        import io
        import csv
        
        contents = await file.read()
        rows = []
        headers = []
        
        if file.filename.endswith('.csv'):
            text_content = contents.decode('utf-8-sig', errors='replace')
            csv_reader = csv.reader(io.StringIO(text_content))
            try:
                headers = [str(h).strip() for h in next(csv_reader)]
                for row_data in csv_reader:
                    # pad row to match headers length
                    row_data = row_data + [''] * (len(headers) - len(row_data))
                    rows.append(dict(zip(headers, row_data)))
            except StopIteration:
                pass
        else:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
            sheet = wb.active
            
            header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
            if header_row:
                headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(header_row)]
                for row_data in sheet.iter_rows(min_row=2, values_only=True):
                    row_data = list(row_data) + [''] * (len(headers) - len(row_data))
                    row_data = [str(cell) if cell is not None else "" for cell in row_data]
                    rows.append(dict(zip(headers, row_data)))
                    
        # Identify columns
        po_col = next((c for c in headers if 'po' in c.lower() and 'id' in c.lower()), None)
        if not po_col:
            po_col = next((c for c in headers if c.lower() in ['po', 'po number', 'purchase order']), None)
            
        sku_col = next((c for c in headers if 'sku' in c.lower() or 'productid' in c.lower() or 'product id' in c.lower()), None)
        qty_col = next((c for c in headers if 'qty' in c.lower() or 'quantity' in c.lower()), None)
        
        if not po_col or not sku_col:
            raise HTTPException(status_code=400, detail=f"Could not identify PO ID or SKU columns. Found columns: {', '.join(headers)}")
            
        results = []
        
        for index, row in enumerate(rows):
            po_val = str(row.get(po_col, "")).strip()
            sku_val = str(row.get(sku_col, "")).strip()
            
            qty_val = 0
            raw_qty = row.get(qty_col)
            if raw_qty:
                try:
                    qty_val = int(float(raw_qty))
                except ValueError:
                    qty_val = 0
                    
            if not po_val or not sku_val:
                continue
                
            # Clean up PO value in case it has # or .0
            import re
            po_val_clean = re.sub(r'[^0-9]', '', po_val)
            if not po_val_clean:
                po_val_clean = po_val
                
            # Lookup PO item in DB
            try:
                sellercloud_po_id = int(po_val_clean)
            except ValueError:
                sellercloud_po_id = -1
                
            # Check PO existence first
            po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id).first()
            po_item = None
            validation_message = "Valid"
            status = "success"
            
            if not po:
                validation_message = f"PO {sellercloud_po_id} not found in database. Try syncing it first."
                status = "error"
            else:
                # PO exists, check SKU
                po_item = (
                    db.query(models.PurchaseOrderItem)
                    .filter(
                        models.PurchaseOrderItem.purchase_order_id == po.id,
                        models.PurchaseOrderItem.sku.ilike(f"%{sku_val}%")
                    )
                    .first()
                )
                if not po_item:
                    validation_message = f"SKU '{sku_val}' not found in PO {sellercloud_po_id}."
                    status = "error"
            
            row_result = {
                "row_index": index + 1,
                "file_po_id": po_val,
                "file_sku": sku_val,
                "file_qty": qty_val,
                "status": status,
                "validation_message": validation_message,
                "found_item": None
            }
            
            if po_item:
                qty_already = sum(link.qty_in_container or 0 for link in po_item.container_links) if po_item.container_links else 0
                qty_available = max(0, po_item.qty_ordered - qty_already)
                
                row_result["found_item"] = {
                    "po_item_id": str(po_item.id),
                    "purchase_order_id": str(po_item.purchase_order_id),
                    "sellercloud_item_id": po_item.sellercloud_item_id,
                    "product_name": po_item.product_name,
                    "qty_ordered": po_item.qty_ordered,
                    "qty_received": po_item.qty_received,
                    "qty_already_in_containers": qty_already,
                    "qty_available_for_container": qty_available
                }
                
            results.append(row_result)
            
        return {
            "success": True,
            "message": f"Processed {len(results)} rows",
            "columns_identified": {
                "po_column": po_col,
                "sku_column": sku_col,
                "quantity_column": qty_col
            },
            "data": results
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

@router.get("/validate-item")
def validate_container_item(
    po_id: str = Query(..., description="The SellerCloud PO ID"),
    sku: str = Query(..., description="The SKU of the item"),
    db: Session = Depends(get_db)
):
    """
    Validates a single PO/SKU pair for a manual row entry on the frontend.
    Returns exactly the same validation structure as the CSV import preview.
    """
    import re
    
    # Clean up PO value in case it has # or .0
    po_val_clean = re.sub(r'[^0-9]', '', str(po_id))
    if not po_val_clean:
        po_val_clean = str(po_id)
        
    try:
        sellercloud_po_id = int(po_val_clean)
    except ValueError:
        sellercloud_po_id = -1
        
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id).first()
    po_item = None
    validation_message = "Valid"
    status = "success"
    
    if not po:
        validation_message = f"PO {sellercloud_po_id} not found in database. Try syncing it first."
        status = "error"
    else:
        # PO exists, check SKU
        po_item = (
            db.query(models.PurchaseOrderItem)
            .filter(
                models.PurchaseOrderItem.purchase_order_id == po.id,
                models.PurchaseOrderItem.sku.ilike(f"%{sku}%")
            )
            .first()
        )
        if not po_item:
            validation_message = f"SKU '{sku}' not found in PO {sellercloud_po_id}."
            status = "error"
            
    result = {
        "file_po_id": str(po_id),
        "file_sku": str(sku),
        "status": status,
        "validation_message": validation_message,
        "found_item": None
    }
    
    if po_item:
        qty_already = sum(link.qty_in_container or 0 for link in po_item.container_links) if po_item.container_links else 0
        qty_available = max(0, po_item.qty_ordered - qty_already)
        
        result["found_item"] = {
            "po_item_id": str(po_item.id),
            "purchase_order_id": str(po_item.purchase_order_id),
            "sellercloud_item_id": po_item.sellercloud_item_id,
            "product_name": po_item.product_name,
            "qty_ordered": po_item.qty_ordered,
            "qty_received": po_item.qty_received,
            "qty_already_in_containers": qty_already,
            "qty_available_for_container": qty_available
        }
        
    return result

@router.post("/validate-items-bulk")
def validate_container_items_bulk(
    request: schemas.ValidateContainerBulkRequest,
    db: Session = Depends(get_db)
):
    """
    Validates multiple PO/SKU pairs for manual row entries on the frontend.
    Returns exactly the same validation structure as the CSV import preview for each row.
    """
    import re
    results = []
    
    for index, item in enumerate(request.items):
        po_id = item.po_id
        sku = item.sku
        qty = item.qty
        
        # Clean up PO value in case it has # or .0
        po_val_clean = re.sub(r'[^0-9]', '', str(po_id))
        if not po_val_clean:
            po_val_clean = str(po_id)
            
        try:
            sellercloud_po_id = int(po_val_clean)
        except ValueError:
            sellercloud_po_id = -1
            
        po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id).first()
        po_item = None
        validation_message = "Valid"
        status = "success"
        
        if not po:
            validation_message = f"PO {sellercloud_po_id} not found in database. Try syncing it first."
            status = "error"
        else:
            # PO exists, check SKU
            po_item = (
                db.query(models.PurchaseOrderItem)
                .filter(
                    models.PurchaseOrderItem.purchase_order_id == po.id,
                    models.PurchaseOrderItem.sku.ilike(f"%{sku}%")
                )
                .first()
            )
            if not po_item:
                validation_message = f"SKU '{sku}' not found in PO {sellercloud_po_id}."
                status = "error"
                
        row_result = {
            "row_index": index + 1,
            "file_po_id": str(po_id),
            "file_sku": str(sku),
            "file_qty": qty,
            "status": status,
            "validation_message": validation_message,
            "found_item": None
        }
        
        if po_item:
            qty_already = sum(link.qty_in_container or 0 for link in po_item.container_links) if po_item.container_links else 0
            qty_available = max(0, po_item.qty_ordered - qty_already)
            
            row_result["found_item"] = {
                "po_item_id": str(po_item.id),
                "purchase_order_id": str(po_item.purchase_order_id),
                "sellercloud_item_id": po_item.sellercloud_item_id,
                "product_name": po_item.product_name,
                "qty_ordered": po_item.qty_ordered,
                "qty_received": po_item.qty_received,
                "remaining_qty": max(0, po_item.qty_ordered - (po_item.qty_received or 0)),
                "qty_already_in_containers": qty_already,
                "qty_available_for_container": qty_available
            }
            
        results.append(row_result)
        
    return {
        "success": True,
        "message": f"Validated {len(results)} rows",
        "data": results
    }
