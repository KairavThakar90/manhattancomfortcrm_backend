from typing import Optional, List, Any
from datetime import datetime, timedelta
import csv
import io
import uuid

from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks, Form, File, UploadFile, Request, Body
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_, cast, String

from app.database import get_db
from app.auth import get_current_user
from app import models, schemas
from app.schemas import PurchaseOrderOut, PaginatedResponse, SyncResponse, POExportRequest, POCommentCreate, POCommentOut, POCommentUpdate, POItemCommentCreate, POItemCommentOut, POStatusUpdate, POItemQuantityUpdate, POItemForContainerOut, POItemBasicOut
from app.services.email_service import send_tag_notification
from app.services.sync_service import sync_purchase_orders, sync_containers, backfill_po_customers, sync_single_po_full, sync_all_db_purchase_orders
from app.services.optimized_sync_service import OptimizedSyncService, get_sync_recommendations
from app.services.activity_service import log_activity

router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"], dependencies=[Depends(get_current_user)])


def po_id_filter_clause(po_id: str):
    """Resolve a path-supplied PO identifier (UUID, raw SellerCloud int ID, or "PO-<id>") to a filter clause."""
    raw_id = po_id[3:] if po_id.upper().startswith("PO-") else po_id
    try:
        return models.PurchaseOrder.id == uuid.UUID(raw_id)
    except ValueError:
        if raw_id.isdigit():
            return models.PurchaseOrder.sellercloud_po_id == int(raw_id)
        raise HTTPException(status_code=400, detail="Invalid PO ID format. Must be a UUID, SellerCloud integer ID, or 'PO-<id>'.")


async def process_comment_tags(db, tagged_user_ids, commenter_name, link, background_tasks, is_edit=False, section="Purchase Orders", po_number=None, sku=None, comment_text="", attachments=None):
    import app.models as models
    import re
    
    explicit_ids = set()
    if tagged_user_ids:
        found_uuids = re.findall(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', str(tagged_user_ids))
        explicit_ids.update(found_uuids)

    if comment_text:
        matches = re.findall(r'@([A-Za-z0-9_\-\.]+)', comment_text)
        if matches:
            users = db.query(models.User).all()
            for match in matches:
                match_clean = match.lower().replace("_", " ")
                for user in users:
                    full_name = (user.full_name or "").lower()
                    first_name = (user.first_name or "").lower()
                    if full_name and match_clean in full_name:
                        explicit_ids.add(str(user.id))
                    elif first_name and match_clean in first_name:
                        explicit_ids.add(str(user.id))

    if not explicit_ids:
        return
        
    users = db.query(models.User).filter(models.User.id.in_(list(explicit_ids))).all()
    emails = [u.email for u in users if u.email]
    if emails:
        background_tasks.add_task(
            send_tag_notification, 
            emails=emails, 
            commenter_name=commenter_name, 
            link=link, 
            is_edit=is_edit, 
            section=section,
            po_number=po_number,
            sku=sku,
            comment_text=comment_text,
            attachments=attachments
        )


@router.get("/filters/all-categories")
def get_all_filter_categories(
    vendor_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Get all filter categories in one response.
    
    Returns 4 objects:
    1. new_arrivals: POs created in last 10 days without invoice
    2. invoice_delayed: POs older than 10 days without invoice  
    3. delivery_overdue: POs where delivery is overdue based on lead time
    4. remaining_items: POs with items not fully received
    
    Each object contains:
    - data: Array of POs (paginated)
    - meta: Pagination info (total, page, page_size, etc.)
    """
    from datetime import datetime, timedelta, timezone
    
    today = datetime.now(timezone.utc).date()
    ten_days_ago = today - timedelta(days=10)
    
    # Base query
    base_query = db.query(models.PurchaseOrder).options(
        joinedload(models.PurchaseOrder.vendor),
        joinedload(models.PurchaseOrder.company),
        joinedload(models.PurchaseOrder.customer),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments),
        joinedload(models.PurchaseOrder.items)
    )
    
    # Apply vendor filter if provided
    if current_user.role == "vendor":
        base_query = base_query.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
    elif vendor_id:
        base_query = base_query.filter(models.PurchaseOrder.vendor_id == models.Vendor.id).filter(models.Vendor.sellercloud_vendor_id == vendor_id)
    
    # 1. NEW ARRIVALS: Created in last 10 days without invoice
    new_arrivals_query = base_query.filter(
        and_(
            models.PurchaseOrder.created_on >= ten_days_ago,
            models.PurchaseOrder.invoice_date.is_(None)
        )
    )
    new_arrivals_total = new_arrivals_query.count()
    new_arrivals_data = new_arrivals_query.offset((page - 1) * page_size).limit(page_size).all()
    
    # 2. INVOICE DELAYED: Older than 10 days without invoice
    invoice_delayed_query = base_query.filter(
        and_(
            models.PurchaseOrder.created_on < ten_days_ago,
            models.PurchaseOrder.invoice_date.is_(None)
        )
    )
    invoice_delayed_total = invoice_delayed_query.count()
    invoice_delayed_data = invoice_delayed_query.offset((page - 1) * page_size).limit(page_size).all()
    
    # 3. DELIVERY OVERDUE: Invoice date + lead time < today
    delivery_overdue_query = base_query.filter(
        and_(
            models.PurchaseOrder.invoice_date.isnot(None),
            models.PurchaseOrder.container_lead_time_days.isnot(None)
        )
    )
    delivery_overdue_pos = []
    for po in delivery_overdue_query.all():
        expected_delivery = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
        if expected_delivery < today:
            delivery_overdue_pos.append(po)
    
    delivery_overdue_total = len(delivery_overdue_pos)
    delivery_overdue_data = delivery_overdue_pos[(page - 1) * page_size : page * page_size]
    
    # 4. REMAINING ITEMS: POs with qty_remaining > 0
    remaining_items_query = base_query.join(models.PurchaseOrderItem).filter(
        models.PurchaseOrderItem.qty_received < models.PurchaseOrderItem.qty_ordered
    ).distinct()
    remaining_items_total = remaining_items_query.count()
    remaining_items_data = remaining_items_query.offset((page - 1) * page_size).limit(page_size).all()
    
    # Helper function to create response object
    def create_response_object(data_list, total):
        validated_pos = []
        for po in data_list:
            try:
                po_dict = PurchaseOrderOut.model_validate(po).model_dump(mode='python', exclude={'items', 'comments'})
                validated_pos.append(po_dict)
            except Exception as e:
                print(f"Error validating PO {po.id}: {e}")
                continue
        
        return {
            "data": validated_pos,
            "meta": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "has_next": page * page_size < total,
                "has_prev": page > 1
            }
        }
    
    # Build response with all 4 categories
    return {
        "new_arrivals": create_response_object(new_arrivals_data, new_arrivals_total),
        "invoice_delayed": create_response_object(invoice_delayed_data, invoice_delayed_total),
        "delivery_overdue": create_response_object(delivery_overdue_data, delivery_overdue_total),
        "remaining_items": create_response_object(remaining_items_data, remaining_items_total)
    }


@router.get("/summary/status-counts")
def get_status_counts(db: Session = Depends(get_db)):
    """
    Get counts of POs by status flags.
    
    Returns:
    - total_pos: Total number of POs
    - delayed_invoice_count: Count of POs with delayed invoices
    - overdue_container_count: Count of POs with overdue containers
    - both_issues_count: Count of POs with both issues
    
    This is more efficient than fetching all POs and counting in frontend.
    """
    from datetime import datetime, timedelta, timezone
    
    # Get all POs
    pos = db.query(models.PurchaseOrder).all()
    
    today = datetime.now(timezone.utc).date()
    
    total_pos = len(pos)
    delayed_invoice_count = 0
    overdue_container_count = 0
    both_issues_count = 0
    
    for po in pos:
        is_invoice_delayed = False
        is_container_overdue = False
        
        # Check invoice delayed
        if po.invoice_date:
            is_invoice_delayed = False
        elif po.created_on:
            days_since_creation = (today - po.created_on.date()).days
            is_invoice_delayed = days_since_creation > 10
        
        # Check container overdue based on PO lead time
        if po.invoice_date and po.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
            is_container_overdue = expected_arrival < today
        
        # Count
        if is_invoice_delayed:
            delayed_invoice_count += 1
        if is_container_overdue:
            overdue_container_count += 1
        if is_invoice_delayed and is_container_overdue:
            both_issues_count += 1
    
    return {
        "total_pos": total_pos,
        "delayed_invoice_count": delayed_invoice_count,
        "overdue_container_count": overdue_container_count,
        "both_issues_count": both_issues_count,
        "summary": {
            "delayed_invoice_percentage": round((delayed_invoice_count / total_pos * 100), 2) if total_pos > 0 else 0,
            "overdue_container_percentage": round((overdue_container_count / total_pos * 100), 2) if total_pos > 0 else 0,
        }
    }


@router.post("/migrate-channels-schema")
def migrate_schema():
    """
    Temporary endpoint to apply the channels schema migration to the live database.
    """
    from sqlalchemy import text
    from app.database import engine
    
    results = []
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS channels (
                        id UUID PRIMARY KEY,
                        name VARCHAR(255) NOT NULL UNIQUE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """))
                conn.commit()
                results.append("Channels table created or already exists.")
            except Exception as e:
                conn.rollback()
                results.append(f"channels table: {str(e)}")
            
            try:
                conn.execute(text("ALTER TABLE purchase_orders ADD COLUMN channel_order_id VARCHAR(255) NULL;"))
                conn.commit()
                results.append("Added channel_order_id.")
            except Exception as e:
                conn.rollback()
                results.append(f"channel_order_id: {str(e)}")

            try:
                conn.execute(text("ALTER TABLE purchase_orders ADD COLUMN channel_id UUID NULL;"))
                conn.commit()
                results.append("Added channel_id.")
            except Exception as e:
                conn.rollback()
                results.append(f"channel_id: {str(e)}")

            try:
                conn.execute(text("""
                    ALTER TABLE purchase_orders 
                    ADD CONSTRAINT fk_purchase_orders_channel_id 
                    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE SET NULL;
                """))
                conn.commit()
                results.append("Added foreign key fk_purchase_orders_channel_id.")
            except Exception as e:
                conn.rollback()
                results.append(f"foreign key: {str(e)}")
                
        return {"success": True, "details": results}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.post("/backfill-channels-customers")
def trigger_backfill(background_tasks: BackgroundTasks):
    """
    Triggers a background task to backfill missing customer_id and channel_id
    on all existing Purchase Orders.
    """
    def run_backfill():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            backfill_po_customers(db)
        finally:
            db.close()
            
    background_tasks.add_task(run_backfill)
    return {"message": "Background backfill task for Channels and Customers has been successfully triggered."}

@router.get("/channel-order-ids", response_model=List[str])
def list_channel_order_ids(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """
    List all distinct channel order IDs (e.g., 'Schuchman', 'NEWMARK / GUTTMAN - LAKEWOOD')
    for dropdowns and filtering.
    """
    results = db.query(models.PurchaseOrder.channel_order_id).filter(
        models.PurchaseOrder.channel_order_id != None
    ).distinct().order_by(models.PurchaseOrder.channel_order_id).all()
    return [r[0] for r in results if r[0]]

@router.get("")
def list_purchase_orders(
    page: Optional[int] = Query(None, ge=1, description="Page number. Leave empty for all."),
    page_size: Optional[int] = Query(None, ge=1, description="Items per page. Leave empty for all."),
    status_code: Optional[int] = Query(None, description="Raw SellerCloud PurchaseOrderStatus code"),
    status: Optional[str] = Query(None, description="Filter by text status (e.g., NOT_STARTED, IN_PRODUCTION)"),
    vendor_id: Optional[str] = Query(None, description="Filter by local Vendor UUID, SC integer ID, or vendor name"),
    company_id: Optional[str] = Query(None, description="Filter by local Company UUID"),
    sellercloud_company_id: Optional[int] = Query(None, description="Filter by SellerCloud Company integer ID"),
    sort_by: Optional[str] = Query(None, description="Field to sort by: created_on, date_ordered, invoice_date, expected_delivery_date, total_amount"),
    sort_order: Optional[str] = Query("desc", description="Sort order: asc or desc"),
    search: Optional[str] = Query(None, description="Search by PO number, order title, or vendor name"),
    date_from: Optional[str] = Query(None, description="Filter POs ordered on or after this date (supports YYYY-MM)"),
    date_to: Optional[str] = Query(None, description="Filter POs ordered on or before this date (supports YYYY-MM)"),
    customer_id: Optional[str] = Query(None, description="Filter by local Customer UUID"),
    channel_id: Optional[str] = Query(None, description="Filter by local Channel UUID"),
    channel_order_id: Optional[str] = Query(None, description="Filter by explicit Channel Order ID (e.g. 'Schuchman')"),
    warehouse_id: Optional[str] = Query(None, description="Filter by Warehouse local UUID, SellerCloud integer ID, or name"),
    sellercloud_warehouse_id: Optional[str] = Query(None, description="Filter by Warehouse UUID or integer ID"),
    is_completed: Optional[bool] = Query(None, description="True for Completed/Received POs, False for Open POs"),
    approved_status: Optional[str] = Query(None, description="Filter by approved status (ontime, pending, delayed)"),
    has_remaining_qty: Optional[bool] = Query(None, description="Filter POs that have items remaining to be added to containers"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    List purchase orders with filtering and sorting.
    
    Sorting options:
    - sort_by: created_on, date_ordered, invoice_date, expected_delivery_date, total_amount
    - sort_order: asc (ascending) or desc (descending, default)
    
    Examples:
    - Newest first: ?sort_by=created_on&sort_order=desc (default)
    - Oldest first: ?sort_by=created_on&sort_order=asc
    - By invoice date: ?sort_by=invoice_date&sort_order=desc
    - By amount: ?sort_by=total_amount&sort_order=desc
    """
    q = db.query(models.PurchaseOrder).options(
        joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
        joinedload(models.PurchaseOrder.vendor),
        joinedload(models.PurchaseOrder.company),
        joinedload(models.PurchaseOrder.customer),
        joinedload(models.PurchaseOrder.delay_reason_user),
        joinedload(models.PurchaseOrder.comments),
        joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
    )
    
    # Filter by Warehouse (accepts UUID, SellerCloud integer ID, or warehouse name)
    if warehouse_id or sellercloud_warehouse_id:
        target_wh = warehouse_id or sellercloud_warehouse_id
        target_wh_str = str(target_wh).strip()
        if target_wh_str.isdigit():
            q = q.join(models.PurchaseOrder.warehouse).filter(
                models.Warehouse.sellercloud_warehouse_id == int(target_wh_str)
            )
        else:
            try:
                import uuid
                wh_uuid = uuid.UUID(target_wh_str)
                q = q.filter(models.PurchaseOrder.warehouse_id == wh_uuid)
            except ValueError:
                q = q.join(models.PurchaseOrder.warehouse).filter(
                    models.Warehouse.name.ilike(f"%{target_wh_str}%")
                )

    # Filter by Vendor
    if vendor_id:
        vendor_id_str = str(vendor_id).strip()
        if vendor_id_str.isdigit():
            q = q.join(models.PurchaseOrder.vendor).filter(
                models.Vendor.sellercloud_vendor_id == int(vendor_id_str)
            )
        else:
            try:
                import uuid
                v_uuid = uuid.UUID(vendor_id_str)
                q = q.filter(models.PurchaseOrder.vendor_id == v_uuid)
            except ValueError:
                q = q.join(models.PurchaseOrder.vendor).filter(
                    models.Vendor.name.ilike(f"%{vendor_id_str}%")
                )

    # Filter by Company
    if company_id or sellercloud_company_id:
        target_comp = company_id or sellercloud_company_id
        target_comp_str = str(target_comp).strip()
        if target_comp_str.isdigit():
            q = q.join(models.PurchaseOrder.company).filter(
                models.Company.sellercloud_company_id == int(target_comp_str)
            )
        else:
            try:
                import uuid
                c_uuid = uuid.UUID(target_comp_str)
                q = q.filter(models.PurchaseOrder.company_id == c_uuid)
            except ValueError:
                q = q.join(models.PurchaseOrder.company).filter(
                    models.Company.name.ilike(f"%{target_comp_str}%")
                )

    # Filter by Customer
    if customer_id:
        try:
            import uuid
            cust_uuid = uuid.UUID(str(customer_id).strip())
            q = q.filter(models.PurchaseOrder.customer_id == cust_uuid)
        except ValueError:
            pass

    if channel_id:
        try:
            import uuid
            q = q.filter(models.PurchaseOrder.channel_id == uuid.UUID(channel_id))
        except ValueError:
            q = q.filter(models.PurchaseOrder.id == uuid.uuid4())  # Return empty if invalid UUID
            
    if channel_order_id:
        q = q.filter(models.PurchaseOrder.channel_order_id.ilike(f"%{channel_order_id}%"))

    if status_code is not None:
        q = q.filter(models.PurchaseOrder.purchase_order_status_code == status_code)
        
    if status is not None or is_completed is not None:
        from sqlalchemy import func
        subq = db.query(
            models.PurchaseOrderItem.purchase_order_id,
            func.sum(models.PurchaseOrderItem.qty_ordered).label('tot_ord'),
            func.sum(models.PurchaseOrderItem.qty_received).label('tot_rec'),
            func.sum(models.PurchaseOrderItem.qty_in_container).label('tot_in_container')
        ).group_by(models.PurchaseOrderItem.purchase_order_id).subquery()
        
        q = q.outerjoin(subq, models.PurchaseOrder.id == subq.c.purchase_order_id)
        
        if is_completed is True:
            q = q.filter(
                func.coalesce(subq.c.tot_ord, 0) > 0,
                func.coalesce(subq.c.tot_rec, 0) >= func.coalesce(subq.c.tot_ord, 0)
            )
        elif is_completed is False:
            q = q.filter(
                (func.coalesce(subq.c.tot_rec, 0) < func.coalesce(subq.c.tot_ord, 0)) | 
                (func.coalesce(subq.c.tot_ord, 0) == 0)
            )

        if status == "SHIPPED":
            q = q.filter(
                func.coalesce(subq.c.tot_ord, 0) > 0,
                func.coalesce(subq.c.tot_in_container, 0) >= func.coalesce(subq.c.tot_ord, 0)
            )
        elif status == "PARTIALLY_SHIPPED":
            q = q.filter(
                func.coalesce(subq.c.tot_ord, 0) > 0,
                func.coalesce(subq.c.tot_in_container, 0) > 0,
                func.coalesce(subq.c.tot_in_container, 0) < func.coalesce(subq.c.tot_ord, 0)
            )
        elif status == "NOT_STARTED":
            q = q.filter(
                (models.PurchaseOrder.status == "NOT_STARTED") | (models.PurchaseOrder.status.is_(None)),
                # Either no items are in container, or the status is explicitly set to NOT_STARTED in DB
                (func.coalesce(subq.c.tot_in_container, 0) == 0) | (func.lower(models.PurchaseOrder.status) == "not_started")
            )
        elif status is not None:
            # For other statuses (e.g. IN_PRODUCTION, DELAYED), only exclude them if fully shipped (dynamic SHIPPED override)
            q = q.filter(
                func.lower(models.PurchaseOrder.status) == status.lower(),
                (func.coalesce(subq.c.tot_in_container, 0) < func.coalesce(subq.c.tot_ord, 0)) |
                (func.coalesce(subq.c.tot_ord, 0) == 0)
            )
    
    
    if current_user.role == "vendor":
        q = q.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
    elif vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
        
    if company_id:
        q = q.filter(models.PurchaseOrder.company_id == company_id)
    if sellercloud_company_id:
        q = q.join(models.Company, models.PurchaseOrder.company_id == models.Company.id).filter(
            models.Company.sellercloud_company_id == sellercloud_company_id
        )
    if customer_id:
        if customer_id == "00000000-0000-0000-0000-000000000000" or customer_id == "0":
            q = q.filter(models.PurchaseOrder.customer_id.is_(None))
        elif customer_id.isdigit():
            q = q.join(models.Customer, models.PurchaseOrder.customer_id == models.Customer.id).filter(
                models.Customer.sellercloud_customer_id == int(customer_id)
            )
        else:
            q = q.filter(models.PurchaseOrder.customer_id == customer_id)
            
    if sellercloud_warehouse_id:
        try:
            import uuid
            w_uuid = uuid.UUID(sellercloud_warehouse_id)
            q = q.filter(models.PurchaseOrder.warehouse_id == w_uuid)
        except ValueError:
            if sellercloud_warehouse_id.isdigit():
                q = q.join(models.Warehouse, models.PurchaseOrder.warehouse_id == models.Warehouse.id).filter(
                    models.Warehouse.sellercloud_warehouse_id == int(sellercloud_warehouse_id)
                )
        
    if search:
        import re
        escaped_search = re.escape(search)
        search_conditions = [
            models.PurchaseOrder.purchase_title.op('~*')(rf"\y{escaped_search}"),
            models.PurchaseOrder.vendor.has(models.Vendor.name.op('~*')(rf"\y{escaped_search}")),
            models.PurchaseOrder.company.has(models.Company.name.ilike(f"{search}%")),
            models.PurchaseOrder.customer.has(models.Customer.first_name.ilike(f"{search}%")),
            models.PurchaseOrder.customer.has(models.Customer.last_name.ilike(f"{search}%")),
            models.PurchaseOrder.channel.has(models.Channel.name.ilike(f"{search}%")),
            models.PurchaseOrder.channel_order_id.ilike(f"{search}%")
        ]
        if search.isdigit():
            search_conditions.append(cast(models.PurchaseOrder.sellercloud_po_id, String).op('~*')(rf"\y{escaped_search}"))
            
        q = q.filter(or_(*search_conditions))
        
    if approved_status:
        cutoff_10_days = datetime.utcnow() - timedelta(days=10)
        status_val = approved_status.strip().lower().replace(" ", "")
        if status_val in ("ontime", "ontimes"):
            q = q.filter(models.PurchaseOrder.invoice_date.isnot(None))
        elif status_val in ("delayed", "delay"):
            q = q.filter(
                and_(
                    models.PurchaseOrder.invoice_date.is_(None),
                    models.PurchaseOrder.created_on < cutoff_10_days
                )
            )
        elif status_val == "pending":
            q = q.filter(
                and_(
                    models.PurchaseOrder.invoice_date.is_(None),
                    or_(
                        models.PurchaseOrder.created_on >= cutoff_10_days,
                        models.PurchaseOrder.created_on.is_(None)
                    )
                )
            )
            
    if has_remaining_qty is not None:
        if has_remaining_qty:
            q = q.filter(
                models.PurchaseOrder.items.any(
                    models.PurchaseOrderItem.qty_ordered > models.PurchaseOrderItem.qty_in_container
                )
            )
        else:
            q = q.filter(
                ~models.PurchaseOrder.items.any(
                    models.PurchaseOrderItem.qty_ordered > models.PurchaseOrderItem.qty_in_container
                )
            )

    if date_from:
        try:
            from datetime import datetime as dt, timezone
            if len(date_from) == 7 and date_from[4] == '-':
                parsed_date_from = dt(int(date_from[:4]), int(date_from[5:7]), 1, tzinfo=timezone.utc)
            else:
                parsed_date_from = dt.fromisoformat(date_from.replace("Z", "+00:00"))
                if parsed_date_from.tzinfo is None:
                    parsed_date_from = parsed_date_from.replace(tzinfo=timezone.utc)
            q = q.filter(models.PurchaseOrder.date_ordered >= parsed_date_from)
        except Exception as e:
            print(f"Exception in date_from: {e}")

    if date_to:
        try:
            import calendar
            from datetime import time, datetime as dt, timezone
            if len(date_to) == 7 and date_to[4] == '-':
                year = int(date_to[:4])
                month = int(date_to[5:7])
                last_day = calendar.monthrange(year, month)[1]
                parsed_date_to = dt(year, month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
            else:
                parsed_date_to = dt.fromisoformat(date_to.replace("Z", "+00:00"))
                if parsed_date_to.tzinfo is None:
                    parsed_date_to = parsed_date_to.replace(tzinfo=timezone.utc)
                if parsed_date_to.time() == time.min:
                    parsed_date_to = dt.combine(parsed_date_to.date(), time(23, 59, 59, 999999)).replace(tzinfo=timezone.utc)
            q = q.filter(models.PurchaseOrder.date_ordered <= parsed_date_to)
        except Exception as e:
            print(f"Exception in date_to: {e}")

    # Apply sorting
    from sqlalchemy import case
    from datetime import timezone
    cutoff_10_days = datetime.now(timezone.utc) - timedelta(days=10)
    
    # 1 = On Time (has invoice)
    # 2 = Pending (no invoice, <= 10 days)
    # 3 = Delay (no invoice, > 10 days)
    is_invoice_delayed_expr = case(
        (
            models.PurchaseOrder.invoice_date.isnot(None),
            1
        ),
        (
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_10_days
            ),
            3
        ),
        else_=2
    )

    sort_field_map = {
        "created_on": models.PurchaseOrder.created_on,
        "date_ordered": models.PurchaseOrder.date_ordered,
        "invoice_date": models.PurchaseOrder.invoice_date,
        "expected_delivery_date": models.PurchaseOrder.expected_delivery_date,
        "total_amount": models.PurchaseOrder.total_amount,
        "is_invoice_delayed": is_invoice_delayed_expr,
    }
    
    sort_field = sort_field_map.get(sort_by, models.PurchaseOrder.date_ordered)
    
    if sort_order and sort_order.lower() == "asc":
        q = q.order_by(sort_field.asc(), models.PurchaseOrder.created_on.desc())
    else:
        q = q.order_by(sort_field.desc(), models.PurchaseOrder.created_on.desc())

    total = q.count()
    
    if page and page_size:
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
    else:
        rows = q.all()
    
    # Convert to Pydantic models BEFORE converting to dicts
    # This ensures model_validate can access container_links
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    
    # Now convert to dicts
    results = []
    for po in po_models:
        po_dict = po.model_dump(mode='python', exclude={'items', 'comments'})
        if not po_dict.get('customer'):
            po_dict['customer'] = {
                "id": None,
                "sellercloud_customer_id": None,
                "company_id": po_dict.get("company_id"),
                "first_name": "Manhattan",
                "last_name": "comfort",
                "email": "",
                "phone": "",
                "billing_city": "",
                "shipping_city": "",
                "updated_at": None
            }
        results.append(po_dict)
    
    # Build response with meta object
    return {
        "total": total,
        "page": page if page else 1,
        "page_size": page_size if page_size else total,
        "meta": {
            "total": total,
            "page": page if page else 1,
            "page_size": page_size if page_size else total,
            "total_pages": (total + page_size - 1) // page_size if (page_size and page_size > 0) else 1,
            "has_next": (page * page_size < total) if (page and page_size) else False,
            "has_prev": (page > 1) if page else False
        },
        "results": results,
    }


@router.get("/{po_id}", response_model=PurchaseOrderOut)
def get_purchase_order(po_id: str, db: Session = Depends(get_db)):
    filter_clause = po_id_filter_clause(po_id)

    po = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.company),
            joinedload(models.PurchaseOrder.customer),
            joinedload(models.PurchaseOrder.delay_reason_user),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments).joinedload(models.PurchaseOrderItemComment.user),
            joinedload(models.PurchaseOrder.comments).joinedload(models.PurchaseOrderComment.user)
        )
        .filter(filter_clause)
        .first()
    )
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
        
    # Map user_name for comments
    for comment in po.comments:
        if comment.user:
            comment.user_name = comment.user.full_name or comment.user.email
            
    # Map user_name for item comments
    for item in po.items:
        for comment in item.comments:
            if comment.user:
                comment.user_name = comment.user.full_name or comment.user.email
            
    return PurchaseOrderOut.model_validate(po)


@router.post("/{po_id}/comments", response_model=POCommentOut)
async def add_po_comment(
    po_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    comment: Optional[str] = Form(None),
    parent_id: Optional[str] = Form(None),
    tagged_user_ids: Optional[str] = Form(None),
    files: list[UploadFile] = File([]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    import json
    from app.config import settings
    from app.services.gcs_service import upload_file_to_gcs
    filter_clause = po_id_filter_clause(po_id)

    po = db.query(models.PurchaseOrder).filter(filter_clause).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    if current_user.role == "vendor":
        if str(po.vendor_id) != str(current_user.vendor_id):
            raise HTTPException(status_code=403, detail="Not authorized to comment on this PO")

    if not comment:
        try:
            body = await request.json()
            comment = body.get("comment")
            parent_id = body.get("parent_id")
            tagged_users_list = body.get("tagged_user_ids", [])
            if isinstance(tagged_users_list, list):
                tagged_user_ids = json.dumps(tagged_users_list)
            else:
                tagged_user_ids = str(tagged_users_list)
        except Exception:
            pass

    if not comment:
        if files:
            comment = ""
        else:
            raise HTTPException(status_code=400, detail="comment field is required")

    try:
        if not tagged_user_ids or tagged_user_ids == "null":
            tagged_users = []
        else:
            tagged_users = json.loads(tagged_user_ids)
            if not isinstance(tagged_users, list):
                tagged_users = [tagged_users]
    except Exception:
        tagged_users = [uid.strip() for uid in tagged_user_ids.split(",") if uid.strip()]

    # Parse parent_id if it's "null" string
    if parent_id == "null" or not parent_id:
        parent_uuid = None
    else:
        try:
            parent_uuid = uuid.UUID(parent_id)
        except ValueError:
            parent_uuid = None

    new_comment = models.PurchaseOrderComment(
        purchase_order_id=po.id,
        user_id=current_user.id,
        comment=comment,
        parent_id=parent_uuid
    )
    db.add(new_comment)
    db.flush()
    db.refresh(new_comment)
    new_comment.user_name = current_user.full_name or current_user.email
    
    # Process Attachments
    uploaded_attachments = []
    email_attachments = []
    
    allowed_types = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv"
    ]
    
    # Check sizes and types first
    for f in files:
        if f.size and f.size > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
        if f.filename and f.content_type:
            if not f.content_type.startswith('image/') and f.content_type not in allowed_types:
                raise HTTPException(status_code=400, detail=f"File type not allowed for {f.filename}. Only images, PDFs, Word docs, and CSVs are permitted.")
            
    for f in files:
        if not f.filename:
            continue
        # Read bytes
        file_bytes = await f.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
            
        from app.services.gcs_service import upload_file_to_gcs
        file_url = await upload_file_to_gcs(file_bytes, f.filename, f.content_type)
        
        att_model = models.PurchaseOrderCommentAttachment(
            comment_id=new_comment.id,
            file_name=f.filename,
            file_url=file_url,
            content_type=f.content_type,
            size=len(file_bytes)
        )
        db.add(att_model)
        uploaded_attachments.append(att_model)
        email_attachments.append({
            "file_name": f.filename,
            "content": file_bytes,
            "content_type": f.content_type
        })
        
    db.commit()
    
    setattr(new_comment, "attachments", uploaded_attachments)
    
    # Process Tags
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?comment_id={new_comment.id}"
    await process_comment_tags(
        db=db,
        tagged_user_ids=tagged_users,
        commenter_name=new_comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=False,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=None,
        comment_text=new_comment.comment,
        attachments=email_attachments
    )
    
    log_activity(db, action="ADD_PO_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"comment_id": str(new_comment.id), "po_number": str(po.sellercloud_po_id) if po else None})
    return new_comment

@router.put("/comments/{comment_id}", response_model=POCommentOut)
async def update_po_comment(
    comment_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    comment_text: Optional[str] = Form(None, alias="comment"),
    tagged_user_ids: Optional[str] = Form(None),
    files: list[UploadFile] = File([]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    from app.config import settings
    comment = db.query(models.PurchaseOrderComment).filter(models.PurchaseOrderComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")
        
    if comment_text is not None:
        comment.comment = comment_text
        
    # Process Tags
    import json
    tagged_users = []
    if tagged_user_ids:
        try:
            tagged_users = json.loads(tagged_user_ids)
            if not isinstance(tagged_users, list):
                tagged_users = [tagged_users]
        except Exception:
            tagged_users = [uid.strip() for uid in tagged_user_ids.split(",") if uid.strip()]
            
    # Process Attachments
    uploaded_attachments = []
    email_attachments = []
    
    allowed_types = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv"
    ]
    
    for f in files:
        if f.size and f.size > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
        if f.filename and f.content_type:
            if not f.content_type.startswith('image/') and f.content_type not in allowed_types:
                raise HTTPException(status_code=400, detail=f"File type not allowed for {f.filename}.")
            
    for f in files:
        if not f.filename:
            continue
        file_bytes = await f.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
            
        from app.services.gcs_service import upload_file_to_gcs
        file_url = await upload_file_to_gcs(file_bytes, f.filename, f.content_type)
        
        att_model = models.PurchaseOrderCommentAttachment(
            comment_id=comment.id,
            file_name=f.filename,
            file_url=file_url,
            content_type=f.content_type,
            size=len(file_bytes)
        )
        db.add(att_model)
        uploaded_attachments.append(att_model)
        email_attachments.append({
            "file_name": f.filename,
            "content": file_bytes,
            "content_type": f.content_type
        })
        
    comment.is_edited = True
    db.commit()
    db.refresh(comment)
    comment.user_name = current_user.full_name or current_user.email
    
    # Process Tags
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == comment.purchase_order_id).first()
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?comment_id={comment.id}" if po else ""
    await process_comment_tags(
        db=db,
        tagged_user_ids=tagged_users,
        commenter_name=comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=True,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=None,
        comment_text=comment.comment,
        attachments=email_attachments
    )
    
    log_activity(db, action="UPDATE_PO_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id) if po else None, details={"comment_id": str(comment.id), "po_number": str(po.sellercloud_po_id) if po else None})
    return comment

@router.delete("/comments/{comment_id}")
def delete_po_comment(
    comment_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    comment = db.query(models.PurchaseOrderComment).filter(models.PurchaseOrderComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")
        
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == comment.purchase_order_id).first()
    
    from app.services.gcs_service import delete_file_from_gcs
    for attachment in comment.attachments:
        if attachment.file_url:
            background_tasks.add_task(delete_file_from_gcs, attachment.file_url)
            
    db.delete(comment)
    db.commit()
    
    log_activity(db, action="DELETE_PO_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id) if po else None, details={"comment_id": comment_id, "po_number": str(po.sellercloud_po_id) if po else None})
    return {"success": True, "message": "Comment deleted successfully"}

@router.delete("/comments/attachments/{attachment_id}")
def delete_po_comment_attachment(
    attachment_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    attachment = db.query(models.PurchaseOrderCommentAttachment).filter(models.PurchaseOrderCommentAttachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
        
    comment = attachment.comment
    if comment and comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this attachment")
        
    from app.services.gcs_service import delete_file_from_gcs
    if attachment.file_url:
        background_tasks.add_task(delete_file_from_gcs, attachment.file_url)
        
    db.delete(attachment)
    db.commit()
    
    return {"success": True, "message": "Attachment deleted successfully"}

@router.get("/items/{item_id}/comments", response_model=list[POItemCommentOut])
def get_po_item_comments(
    item_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    import uuid
    if item_id.isdigit():
        filter_clause = models.PurchaseOrderItem.sellercloud_item_id == int(item_id)
    else:
        try:
            item_uuid = uuid.UUID(item_id)
            filter_clause = models.PurchaseOrderItem.id == item_uuid
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Item ID format. Must be a UUID or SellerCloud integer ID.")
            
    item = db.query(models.PurchaseOrderItem).filter(filter_clause).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    comments = (
        db.query(models.PurchaseOrderItemComment)
        .options(joinedload(models.PurchaseOrderItemComment.user))
        .filter(models.PurchaseOrderItemComment.purchase_order_item_id == item.id)
        .order_by(models.PurchaseOrderItemComment.created_at.asc())
        .all()
    )
    for comment in comments:
        comment.user_name = comment.user.full_name or comment.user.email if comment.user else None
    return comments


@router.post("/items/{item_id}/comments", response_model=POItemCommentOut)
async def add_po_item_comment(
    item_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    comment: Optional[str] = Form(None),
    parent_id: Optional[str] = Form(None),
    tagged_user_ids: Optional[str] = Form(None),
    files: list[UploadFile] = File([]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    import json
    import uuid
    from app.config import settings
    from app.services.gcs_service import upload_file_to_gcs
    
    if item_id.isdigit():
        filter_clause = models.PurchaseOrderItem.sellercloud_item_id == int(item_id)
    else:
        try:
            item_uuid = uuid.UUID(item_id)
            filter_clause = models.PurchaseOrderItem.id == item_uuid
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Item ID format. Must be a UUID or SellerCloud integer ID.")
            
    item = db.query(models.PurchaseOrderItem).filter(filter_clause).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
        
    if not comment:
        try:
            body = await request.json()
            comment = body.get("comment")
            parent_id = body.get("parent_id")
            tagged_users_list = body.get("tagged_user_ids", [])
            if isinstance(tagged_users_list, list):
                tagged_user_ids = json.dumps(tagged_users_list)
            else:
                tagged_user_ids = str(tagged_users_list)
        except Exception:
            pass

    if not comment:
        if files:
            comment = ""
        else:
            raise HTTPException(status_code=400, detail="comment field is required")
        
    try:
        if not tagged_user_ids or tagged_user_ids == "null":
            tagged_users = []
        else:
            tagged_users = json.loads(tagged_user_ids)
            if not isinstance(tagged_users, list):
                tagged_users = [tagged_users]
    except Exception:
        tagged_users = [uid.strip() for uid in tagged_user_ids.split(",") if uid.strip()]

    if parent_id == "null" or not parent_id:
        parent_uuid = None
    else:
        try:
            parent_uuid = uuid.UUID(parent_id)
        except ValueError:
            parent_uuid = None

    new_comment = models.PurchaseOrderItemComment(
        purchase_order_item_id=item.id,
        user_id=current_user.id,
        comment=comment,
        parent_id=parent_uuid
    )
    db.add(new_comment)
    db.flush()
    db.refresh(new_comment)
    new_comment.user_name = current_user.full_name or current_user.email
    
    # Process Attachments
    uploaded_attachments = []
    email_attachments = []
    
    allowed_types = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv"
    ]
    
    for f in files:
        if f.size and f.size > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
        if f.filename and f.content_type:
            if not f.content_type.startswith('image/') and f.content_type not in allowed_types:
                raise HTTPException(status_code=400, detail=f"File type not allowed for {f.filename}. Only images, PDFs, Word docs, and CSVs are permitted.")
            
    for f in files:
        if not f.filename:
            continue
        file_bytes = await f.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
            
        from app.services.gcs_service import upload_file_to_gcs
        file_url = await upload_file_to_gcs(file_bytes, f.filename, f.content_type)
        
        att_model = models.PurchaseOrderItemCommentAttachment(
            comment_id=new_comment.id,
            file_name=f.filename,
            file_url=file_url,
            content_type=f.content_type,
            size=len(file_bytes)
        )
        db.add(att_model)
        uploaded_attachments.append(att_model)
        email_attachments.append({
            "file_name": f.filename,
            "content": file_bytes,
            "content_type": f.content_type
        })
        
    db.commit()
    
    setattr(new_comment, "attachments", uploaded_attachments)
    
    # Process Tags
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == item.purchase_order_id).first() if item else None
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?item_id={item.sellercloud_item_id}&comment_id={new_comment.id}" if po else ""
    await process_comment_tags(
        db=db,
        tagged_user_ids=tagged_users,
        commenter_name=new_comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=False,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=item.sku if item else None,
        comment_text=new_comment.comment,
        attachments=email_attachments
    )
    
    log_activity(db, action="ADD_PO_ITEM_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER_ITEM", entity_id=str(item.id), details={"comment_id": str(new_comment.id), "po_number": str(po.sellercloud_po_id) if po else None})
    return new_comment

@router.put("/items/comments/{comment_id}", response_model=POItemCommentOut)
async def update_po_item_comment(
    comment_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    comment_text: Optional[str] = Form(None, alias="comment"),
    tagged_user_ids: Optional[str] = Form(None),
    files: list[UploadFile] = File([]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    from app.config import settings
    comment = db.query(models.PurchaseOrderItemComment).filter(models.PurchaseOrderItemComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")
        
    if comment_text is not None:
        comment.comment = comment_text
        
    # Process Tags
    import json
    tagged_users = []
    if tagged_user_ids:
        try:
            tagged_users = json.loads(tagged_user_ids)
            if not isinstance(tagged_users, list):
                tagged_users = [tagged_users]
        except Exception:
            tagged_users = [uid.strip() for uid in tagged_user_ids.split(",") if uid.strip()]
            
    # Process Attachments
    uploaded_attachments = []
    email_attachments = []
    
    allowed_types = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv"
    ]
    
    for f in files:
        if f.size and f.size > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
        if f.filename and f.content_type:
            if not f.content_type.startswith('image/') and f.content_type not in allowed_types:
                raise HTTPException(status_code=400, detail=f"File type not allowed for {f.filename}.")
            
    for f in files:
        if not f.filename:
            continue
        file_bytes = await f.read()
        if len(file_bytes) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 5MB limit.")
            
        from app.services.gcs_service import upload_file_to_gcs
        file_url = await upload_file_to_gcs(file_bytes, f.filename, f.content_type)
        
        att_model = models.PurchaseOrderItemCommentAttachment(
            comment_id=comment.id,
            file_name=f.filename,
            file_url=file_url,
            content_type=f.content_type,
            size=len(file_bytes)
        )
        db.add(att_model)
        uploaded_attachments.append(att_model)
        email_attachments.append({
            "file_name": f.filename,
            "content": file_bytes,
            "content_type": f.content_type
        })
        
    comment.is_edited = True
    db.commit()
    db.refresh(comment)
    comment.user_name = current_user.full_name or current_user.email
    
    # Process Tags
    item = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.id == comment.purchase_order_item_id).first()
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == item.purchase_order_id).first() if item else None
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?item_id={item.sellercloud_item_id}&comment_id={comment.id}" if po else ""
    await process_comment_tags(
        db=db,
        tagged_user_ids=tagged_users,
        commenter_name=comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=True,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=item.sku if item else None,
        comment_text=comment.comment,
        attachments=email_attachments
    )
    
    log_activity(db, action="UPDATE_PO_ITEM_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER_ITEM", entity_id=str(item.id) if item else None, details={"comment_id": str(comment.id), "po_number": str(po.sellercloud_po_id) if po else None})
    return comment

@router.delete("/items/comments/{comment_id}")
def delete_po_item_comment(
    comment_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    comment = db.query(models.PurchaseOrderItemComment).filter(models.PurchaseOrderItemComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment")
        
    item = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.id == comment.purchase_order_item_id).first()
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == item.purchase_order_id).first() if item else None
    
    from app.services.gcs_service import delete_file_from_gcs
    for attachment in comment.attachments:
        if attachment.file_url:
            background_tasks.add_task(delete_file_from_gcs, attachment.file_url)
            
    db.delete(comment)
    db.commit()
    
    log_activity(db, action="DELETE_PO_ITEM_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER_ITEM", entity_id=str(item.id) if item else None, details={"comment_id": comment_id, "po_number": str(po.sellercloud_po_id) if po else None})
    return {"success": True, "message": "Item comment deleted successfully"}

@router.delete("/items/comments/attachments/{attachment_id}")
def delete_po_item_comment_attachment(
    attachment_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    attachment = db.query(models.PurchaseOrderItemCommentAttachment).filter(models.PurchaseOrderItemCommentAttachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
        
    comment = attachment.comment
    if comment and comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this attachment")
        
    from app.services.gcs_service import delete_file_from_gcs
    if attachment.file_url:
        background_tasks.add_task(delete_file_from_gcs, attachment.file_url)
        
    db.delete(attachment)
    db.commit()
    
    return {"success": True, "message": "Attachment deleted successfully"}

def auto_sync_po_background(po_id: int):
    import logging
    import time
    from app.database import SessionLocal
    from app.models import User
    
    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        logger.info(f"Auto-syncing PO {po_id} in background...")
        # Give the server a moment to finish the current transaction
        time.sleep(2)
        
        # Fetch a system user to pass for activity logging
        system_user = db.query(User).filter(User.email == "googlecloudcron@manhattancomfort.com").first()
        if not system_user:
            system_user = db.query(User).first()
            
        # 1. Sync the single PO (to get updated totals)
        from app.routers.purchase_orders import trigger_single_po_sync, trigger_container_sync
        trigger_single_po_sync(sellercloud_po_id=po_id, current_user=system_user, db=db)
        
        # 2. Sync containers
        trigger_container_sync(sellercloud_po_id=po_id, db=db)
        
        logger.info(f"Auto-sync complete for PO {po_id}")
    except Exception as e:
        logger.error(f"Auto-sync failed for PO {po_id}: {e}")
    finally:
        db.close()

@router.patch("/items/{item_id}/quantity", response_model=POItemBasicOut)
def update_po_item_quantity(
    item_id: str,
    update_data: POItemQuantityUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    import uuid
    from app.services.sellercloud_client import SellerCloudClient

    try:
        item_uuid = uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid item ID format")

    item = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.id == item_uuid).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == item.purchase_order_id).first()
    if not po:
        raise HTTPException(status_code=404, detail="Parent PO not found")
        
    if not po.sellercloud_po_id or not item.sellercloud_item_id:
        raise HTTPException(status_code=400, detail="Item is not properly linked to SellerCloud")

    sc_client = SellerCloudClient()
    try:
        success = sc_client.update_purchase_order_item_quantity(
            po_id=po.sellercloud_po_id, 
            item_id=item.sellercloud_item_id, 
            new_qty=update_data.qty_ordered,
            unit_price=float(item.unit_price) if item.unit_price else 0.0
        )
        if not success:
            raise HTTPException(status_code=500, detail="SellerCloud returned an unsuccessful status")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update SellerCloud: {str(e)}")

    old_qty = item.qty_ordered
    item.qty_ordered = update_data.qty_ordered
    db.commit()
    db.refresh(item)
    
    log_activity(db, action="UPDATE_PO_ITEM_QUANTITY", user_id=current_user.id, entity_type="PURCHASE_ORDER_ITEM", entity_id=str(item.id), details={"old_qty": old_qty, "new_qty": item.qty_ordered})
    
    # Auto-trigger sync in background
    background_tasks.add_task(auto_sync_po_background, po.sellercloud_po_id)
    
    return item


@router.get("/filters/all")
def get_filtered_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    filter_type: Optional[str] = Query(None, description="Filter type: new_without_invoice, invoice_delayed, delivery_overdue, remaining_items. If not provided, returns all 4 categories."),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    customer_id: Optional[str] = Query(None, description="Filter by customer UUID or SC ID"),
    channel_id: Optional[str] = Query(None, description="Filter by channel UUID"),
    channel_order_id: Optional[str] = Query(None, description="Filter by channel order ID"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Unified filter endpoint for purchase orders.
    
    **If filter_type is NOT provided**, returns all 4 categories in one response:
    {
      "new_arrivals": { data: [...], meta: {...} },
      "invoice_delayed": { data: [...], meta: {...} },
      "delivery_overdue": { data: [...], meta: {...} },
      "remaining_items": { data: [...], meta: {...} }
    }
    
    **If filter_type IS provided**, returns single category:
    - new_without_invoice: POs created in last 10 days without invoice
    - invoice_delayed: POs older than 10 days without invoice
    - delivery_overdue: POs where delivery is overdue (invoice_date + lead_time < today)
    - remaining_items: POs with items not fully received (qty_remaining > 0)
    
    Optional vendor_id parameter works with all modes.
    
    Examples:
    - All categories: GET /api/v1/purchase-orders/filters/all
    - Single filter: GET /api/v1/purchase-orders/filters/all?filter_type=delivery_overdue
    - With vendor: GET /api/v1/purchase-orders/filters/all?vendor_id=xxx
    """
    from datetime import timezone
    
    cutoff_10_days = datetime.now(timezone.utc) - timedelta(days=10)
    today = datetime.now(timezone.utc).date()
    
    # If no filter_type, return all 4 categories
    if filter_type is None:
        # Base query
        base_q = (
            db.query(models.PurchaseOrder)
            .options(
                joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
                joinedload(models.PurchaseOrder.vendor),
                joinedload(models.PurchaseOrder.company),
        joinedload(models.PurchaseOrder.customer),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
            )
        )
        
        # Apply vendor filter if provided
        if current_user.role == "vendor":
            base_q = base_q.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
        elif vendor_id:
            base_q = base_q.filter(models.PurchaseOrder.vendor_id == vendor_id)
            
        if customer_id:
            if customer_id == "00000000-0000-0000-0000-000000000000" or customer_id == "0":
                base_q = base_q.filter(models.PurchaseOrder.customer_id.is_(None))
            elif customer_id.isdigit():
                base_q = base_q.join(models.Customer, models.PurchaseOrder.customer_id == models.Customer.id).filter(
                    models.Customer.sellercloud_customer_id == int(customer_id)
                )
            else:
                base_q = base_q.filter(models.PurchaseOrder.customer_id == customer_id)
                
        if channel_id:
            try:
                import uuid
                base_q = base_q.filter(models.PurchaseOrder.channel_id == uuid.UUID(channel_id))
            except ValueError:
                base_q = base_q.filter(models.PurchaseOrder.id == uuid.uuid4())
                
        if channel_order_id:
            base_q = base_q.filter(models.PurchaseOrder.channel_order_id == channel_order_id)
        
        # Helper function to create response object
        def create_category_response(data_list, total):
            po_models = [PurchaseOrderOut.model_validate(r) for r in data_list]
            results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
            
            return {
                "data": results,
                "meta": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
                    "has_next": page * page_size < total,
                    "has_prev": page > 1
                }
            }
        
        # 1. NEW ARRIVALS (new_without_invoice)
        q1 = base_q.filter(
            and_(
                models.PurchaseOrder.created_on >= cutoff_10_days,
                models.PurchaseOrder.invoice_date.is_(None)
            )
        ).order_by(models.PurchaseOrder.created_on.desc())
        new_arrivals_total = q1.count()
        new_arrivals_data = q1.offset((page - 1) * page_size).limit(page_size).all()
        
        # 2. INVOICE DELAYED
        q2 = base_q.filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_10_days
            )
        ).order_by(models.PurchaseOrder.created_on.asc())
        invoice_delayed_total = q2.count()
        invoice_delayed_data = q2.offset((page - 1) * page_size).limit(page_size).all()
        
        # 3. DELIVERY OVERDUE
        q3 = base_q.filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None)
            )
        )
        all_pos_for_overdue = q3.all()
        overdue_pos = []
        for po in all_pos_for_overdue:
            if po.invoice_date and po.container_lead_time_days:
                expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
                if expected_arrival < today:
                    overdue_pos.append(po)
        overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
        delivery_overdue_total = len(overdue_pos)
        delivery_overdue_data = overdue_pos[(page - 1) * page_size : page * page_size]
        
        # 4. REMAINING ITEMS
        q4 = base_q.join(models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id)
        all_pos_for_remaining = q4.distinct().all()
        pos_with_remaining = []
        for po in all_pos_for_remaining:
            total_remaining = sum((item.qty_ordered or 0) - (item.qty_received or 0) for item in po.items)
            if total_remaining > 0:
                pos_with_remaining.append(po)
        pos_with_remaining.sort(key=lambda po: po.created_on or datetime.min, reverse=True)
        remaining_items_total = len(pos_with_remaining)
        remaining_items_data = pos_with_remaining[(page - 1) * page_size : page * page_size]
        
        # Return all 4 categories
        return {
            "new_arrivals": create_category_response(new_arrivals_data, new_arrivals_total),
            "invoice_delayed": create_category_response(invoice_delayed_data, invoice_delayed_total),
            "delivery_overdue": create_category_response(delivery_overdue_data, delivery_overdue_total),
            "remaining_items": create_category_response(remaining_items_data, remaining_items_total)
        }
    
    # If filter_type is provided, return single category (existing behavior)
    # Base query
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.company),
        joinedload(models.PurchaseOrder.customer),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
    )
    
    # Apply vendor filter if provided
    if current_user.role == "vendor":
        q = q.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
    elif vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
        
    if customer_id:
        if customer_id == "00000000-0000-0000-0000-000000000000" or customer_id == "0":
            q = q.filter(models.PurchaseOrder.customer_id.is_(None))
        elif customer_id.isdigit():
            q = q.join(models.Customer, models.PurchaseOrder.customer_id == models.Customer.id).filter(
                models.Customer.sellercloud_customer_id == int(customer_id)
            )
        else:
            q = q.filter(models.PurchaseOrder.customer_id == customer_id)
            
    if channel_order_id:
        q = q.filter(models.PurchaseOrder.channel_order_id == channel_order_id)
    
    # Apply filter type
    if filter_type == "new_without_invoice":
        # POs created in last 10 days without invoice
        q = q.filter(
            and_(
                models.PurchaseOrder.created_on >= cutoff_10_days,
                models.PurchaseOrder.invoice_date.is_(None)
            )
        )
        q = q.order_by(models.PurchaseOrder.created_on.desc())
        
        # Execute query and paginate
        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
        
    elif filter_type == "invoice_delayed":
        # POs older than 10 days without invoice
        q = q.filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_10_days
            )
        )
        q = q.order_by(models.PurchaseOrder.created_on.asc())
        
        # Execute query and paginate
        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
        
    elif filter_type == "delivery_overdue":
        # POs where delivery is overdue (requires calculation)
        q = q.filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None)
            )
        )
        
        # Get all matching POs and filter by calculation
        all_pos = q.all()
        overdue_pos = []
        for po in all_pos:
            if po.invoice_date and po.container_lead_time_days:
                expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
                if expected_arrival < today:
                    overdue_pos.append(po)
        
        # Sort by how overdue
        overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
        
        # Paginate
        total = len(overdue_pos)
        start = (page - 1) * page_size
        end = start + page_size
        rows = overdue_pos[start:end]
        
    elif filter_type == "remaining_items":
        # POs with remaining items (requires calculation)
        q = q.join(models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id)
        all_pos = q.distinct().all()
        
        # Filter POs with remaining items
        pos_with_remaining = []
        for po in all_pos:
            total_remaining = sum(
                (item.qty_ordered or 0) - (item.qty_received or 0)
                for item in po.items
            )
            if total_remaining > 0:
                pos_with_remaining.append(po)
        
        # Sort by created_on descending
        pos_with_remaining.sort(key=lambda po: po.created_on or datetime.min, reverse=True)
        
        # Paginate
        total = len(pos_with_remaining)
        start = (page - 1) * page_size
        end = start + page_size
        rows = pos_with_remaining[start:end]
        
    else:
        # No filter - return all POs (with vendor filter if provided)
        q = q.order_by(models.PurchaseOrder.date_ordered.desc())
        total = q.count()
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
    
    # Convert to response format
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "filter_type": filter_type or "all",
        "vendor_id": vendor_id,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/new-without-invoice")
def get_new_pos_without_invoice(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs that arrived in the last 10 days and have no invoice date.
    
    Filters:
    - created_on within last 10 days
    - invoice_date is NULL
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/new-without-invoice
    Example with vendor: GET /api/v1/purchase-orders/filters/new-without-invoice?vendor_id=xxx
    """
    from datetime import timezone
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=10)
    
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
        .filter(
            and_(
                models.PurchaseOrder.created_on >= cutoff_date,
                models.PurchaseOrder.invoice_date.is_(None)
            )
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.created_on.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/invoice-delayed")
def get_invoice_delayed_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs with delayed invoices (no invoice date after 10 days from creation).
    
    Filters:
    - invoice_date is NULL
    - created_on is more than 10 days ago
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/invoice-delayed
    Example with vendor: GET /api/v1/purchase-orders/filters/invoice-delayed?vendor_id=xxx
    """
    from datetime import timezone
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=10)
    
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_date
            )
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.created_on.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/delivery-overdue")
def get_delivery_overdue_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs with overdue deliveries (container overdue based on invoice_date + PO lead time).
    
    Filters:
    - invoice_date exists
    - PO has container_lead_time_days set
    - (invoice_date + lead_time_days) < today
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/delivery-overdue
    Example with vendor: GET /api/v1/purchase-orders/filters/delivery-overdue?vendor_id=xxx
    """
    from datetime import timezone
    
    today = datetime.now(timezone.utc).date()
    
    # Get all POs with invoice dates and PO lead times
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None),
            )
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    # Filter in Python since we need to calculate invoice_date + lead_time
    overdue_pos = []
    for po in q.all():
        if po.invoice_date and po.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
            if expected_arrival < today:
                overdue_pos.append(po)
    
    # Sort by how overdue they are (oldest expected arrival first)
    overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
    
    # Paginate
    total = len(overdue_pos)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = overdue_pos[start:end]
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in paginated_pos]
    results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/filters/remaining-items")
def get_pos_with_remaining_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
    db: Session = Depends(get_db),
):
    """
    Get POs that have remaining items (qty_remaining > 0).
    
    Filters:
    - total_qty_remaining > 0 (items not fully received)
    - Optional: filter by vendor_id
    
    Example: GET /api/v1/purchase-orders/filters/remaining-items
    Example with vendor: GET /api/v1/purchase-orders/filters/remaining-items?vendor_id=xxx
    """
    q = (
        db.query(models.PurchaseOrder)
        .join(models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
    )
    
    if vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
    # Filter POs where at least one item has qty_ordered > qty_received
    # We need to check this in Python since it's calculated
    all_pos = q.distinct().all()
    
    pos_with_remaining = []
    for po in all_pos:
        total_remaining = sum(
            item.qty_ordered - item.qty_received 
            for item in po.items
        )
        if total_remaining > 0:
            pos_with_remaining.append(po)
    
    # Sort by created_on descending
    pos_with_remaining.sort(key=lambda po: po.created_on or datetime.min, reverse=True)
    
    # Paginate
    total = len(pos_with_remaining)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = pos_with_remaining[start:end]
    
    po_models = [PurchaseOrderOut.model_validate(r) for r in paginated_pos]
    results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": results,
    }


@router.get("/flags/missing-invoice", response_model=PaginatedResponse)
def get_pos_missing_invoice(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    days_threshold: int = Query(10, ge=1, description="Number of days after creation to flag PO without invoice"),
    db: Session = Depends(get_db),
):
    """
    Get purchase orders that don't have an invoice date after X days (default 10).
    This helps identify POs that need follow-up for payment/invoice.
    
    Flags POs where:
    - invoice_date is NULL
    - created_on is more than X days ago
    """
    cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)
    
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.is_(None),
                models.PurchaseOrder.created_on <= cutoff_date
            )
        )
    )
    
    total = q.count()
    rows = (
        q.order_by(models.PurchaseOrder.created_on.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": [PurchaseOrderOut.model_validate(r).model_dump(mode='python', exclude={'items', 'comments'}) for r in rows],
    }


@router.get("/flags/overdue-containers")
def get_overdue_containers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Get purchase orders where the first container is overdue based on:
    - invoice_date + PO's container_lead_time_days
    
    This flags POs where:
    - invoice_date exists
    - PO has container_lead_time_days set
    - (invoice_date + lead_time_days) < today
    
    Example: If invoice date is Jan 1 and PO lead time is 45 days,
    the container is expected by Feb 15. If today is Feb 20, this PO is flagged.
    """
    today = datetime.utcnow().date()
    
    # Get all POs with invoice dates and PO lead times
    q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
        .filter(
            and_(
                models.PurchaseOrder.invoice_date.isnot(None),
                models.PurchaseOrder.container_lead_time_days.isnot(None),
            )
        )
    )
    
    # Filter in Python since we need to calculate invoice_date + lead_time
    overdue_pos = []
    for po in q.all():
        if po.invoice_date and po.container_lead_time_days:
            expected_arrival = po.invoice_date.date() + timedelta(days=po.container_lead_time_days)
            if expected_arrival < today:
                overdue_pos.append(po)
    
    # Sort by how overdue they are (oldest expected arrival first)
    overdue_pos.sort(key=lambda po: po.invoice_date.date() + timedelta(days=po.container_lead_time_days))
    
    # Paginate
    total = len(overdue_pos)
    start = (page - 1) * page_size
    end = start + page_size
    paginated_pos = overdue_pos[start:end]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "meta": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
            "has_next": page * page_size < total,
            "has_prev": page > 1
        },
        "results": [PurchaseOrderOut.model_validate(r).model_dump(mode='python', exclude={'items', 'comments'}) for r in paginated_pos],
    }


@router.patch("/{po_id}/status", response_model=PurchaseOrderOut)
def update_po_status(
    po_id: str,
    status_data: POStatusUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Update the production or shipment status of a PO.
    Vendors can only update POs assigned to their vendor_id.
    """
    po = None
    try:
        sc_po_id = int(po_id)
        po = (
            db.query(models.PurchaseOrder)
            .options(
                joinedload(models.PurchaseOrder.vendor),
                joinedload(models.PurchaseOrder.delay_reason_user)
            )
            .filter(models.PurchaseOrder.sellercloud_po_id == sc_po_id)
            .first()
        )
    except ValueError:
        pass

    if not po:
        try:
            val_uuid = uuid.UUID(str(po_id))
            po = (
                db.query(models.PurchaseOrder)
                .options(
                    joinedload(models.PurchaseOrder.vendor),
                    joinedload(models.PurchaseOrder.delay_reason_user)
                )
                .filter(models.PurchaseOrder.id == val_uuid)
                .first()
            )
        except Exception:
            pass
            
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase order '{po_id}' not found")
        
    if current_user.role == "vendor":
        if str(po.vendor_id) != str(current_user.vendor_id):
            raise HTTPException(status_code=403, detail="Not authorized to update this PO")
            
    changed = False
    old_status = po.status
    changes = []
    
    if status_data.status is not None and po.status != status_data.status:
        changes.append({"field": "status", "old": po.status, "new": status_data.status})
        po.status = status_data.status
        changed = True
        
    if status_data.delay_reason is not None:
        if po.delay_reason != status_data.delay_reason or po.delay_reason_updated_by_id is None:
            changes.append({"field": "delay_reason", "old": po.delay_reason, "new": status_data.delay_reason})
            po.delay_reason = status_data.delay_reason
            po.delay_reason_updated_by_id = current_user.id
            po.delay_reason_updated_at = datetime.utcnow()
            po.delay_reason_user = current_user
            changed = True
        
    if changed:
        db.commit()
        db.refresh(po)
        log_activity(db, action="UPDATE_PO_STATUS", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"changes": changes})
        
        # Send email notification if vendor or admin made the change
        if current_user.role in ("vendor", "admin"):
            from app.services.email_service import send_po_status_update_email
            updater_name = current_user.full_name or current_user.email
            if current_user.role == "vendor" and po.vendor:
                updater_name = po.vendor.name
            background_tasks.add_task(
                send_po_status_update_email,
                db=db,
                po_number=str(po.sellercloud_po_id),
                old_status=old_status,
                new_status=po.status,
                updater_name=updater_name,
                updater_role=current_user.role
            )
            
    # Re-fetch with loaded relationships for response serialization
    target_po = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.company),
            joinedload(models.PurchaseOrder.customer),
            joinedload(models.PurchaseOrder.delay_reason_user),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
        .filter(models.PurchaseOrder.id == po.id)
        .first()
    ) or po
    return PurchaseOrderOut.model_validate(target_po)

@router.patch("/{po_id}/lead-time")
def update_po_lead_time(
    po_id: str,
    container_lead_time_days: int = Query(..., description="Container lead time in days"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Update container lead time for a specific purchase order.
    
    Accepts PO UUID string or SellerCloud PO ID integer (like 11880).
    
    Lead time is the number of days from invoice date to expected container arrival.
    This is set per PO, not per vendor, for more granular control.
    
    Example: 45 days means container arrives 45 days after invoice date.
    """
    if current_user.role == "vendor":
        raise HTTPException(status_code=403, detail="Vendors cannot update lead time")

    po = None
    # Try to parse as integer (sellercloud_po_id)
    try:
        sc_po_id = int(po_id)
        po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sc_po_id).first()
    except ValueError:
        # Not an integer, lookup by UUID
        try:
            po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
        except Exception:
            pass
            
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase order '{po_id}' not found")
    
    old_lead_time = po.container_lead_time_days
    if old_lead_time != container_lead_time_days:
        po.container_lead_time_days = container_lead_time_days
        db.commit()
        db.refresh(po)
        
        changes = [{"field": "container_lead_time_days", "old": old_lead_time, "new": container_lead_time_days}]
        log_activity(db, action="UPDATE_PO_LEAD_TIME", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"changes": changes})
    
    return {
        "message": "Lead time updated successfully",
        "po_id": str(po.id),
        "sellercloud_po_id": po.sellercloud_po_id,
        "container_lead_time_days": po.container_lead_time_days
    }


@router.patch("/bulk/warehouse")
def update_bulk_po_warehouse(
    data: Optional[schemas.BulkPOWarehouseUpdate] = Body(None),
    po_ids: Optional[List[str]] = Query(None, description="List of PO UUIDs or SellerCloud PO IDs (as query params, alternative to JSON body)"),
    warehouse_id: Optional[str] = Query(None, description="UUID of the new warehouse (as query param, alternative to JSON body)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Update receiving warehouse for multiple purchase orders in both local DB and SellerCloud.

    Accepts po_ids and warehouse_id either as a JSON body ({"po_ids": [...], "warehouse_id": "..."})
    or as query parameters (?po_ids=1&po_ids=2&warehouse_id=...).
    """
    if current_user.role == "vendor":
        raise HTTPException(status_code=403, detail="Vendors cannot update warehouse")

    resolved_po_ids = data.po_ids if data else po_ids
    resolved_warehouse_id = data.warehouse_id if data else warehouse_id

    if not resolved_po_ids:
        raise HTTPException(status_code=422, detail="po_ids is required (in JSON body or as query params)")
    if not resolved_warehouse_id:
        raise HTTPException(status_code=422, detail="warehouse_id is required (in JSON body or as a query param)")

    try:
        import uuid
        w_uuid = uuid.UUID(resolved_warehouse_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid warehouse_id format (must be UUID)")

    warehouse = db.query(models.Warehouse).filter(models.Warehouse.id == w_uuid).first()
    if not warehouse:
        raise HTTPException(status_code=404, detail=f"Warehouse '{resolved_warehouse_id}' not found")

    if not warehouse.sellercloud_warehouse_id:
        raise HTTPException(status_code=400, detail="Warehouse must have a SellerCloud ID to sync")

    from app.services.sellercloud_client import sellercloud_client
    
    updated_pos = []
    failed_pos = []

    for po_id in resolved_po_ids:
        po = None
        # Try to parse as integer (sellercloud_po_id)
        try:
            sc_po_id = int(po_id)
            po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sc_po_id).first()
        except ValueError:
            # Not an integer, lookup by UUID
            try:
                po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == uuid.UUID(po_id)).first()
            except Exception:
                pass
                
        if not po or not po.sellercloud_po_id:
            failed_pos.append({"po_id": po_id, "reason": "Not found or missing SC ID"})
            continue

        try:
            success = sellercloud_client.update_purchase_order_warehouse(po.sellercloud_po_id, warehouse.sellercloud_warehouse_id)
            if success:
                old_warehouse_id = str(po.warehouse_id) if po.warehouse_id else None
                po.warehouse_id = warehouse.id
                
                changes = [{"field": "warehouse_id", "old": old_warehouse_id, "new": str(warehouse.id)}]
                log_activity(db, action="UPDATE_PO_WAREHOUSE", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"changes": changes, "warehouse_name": warehouse.name})
                updated_pos.append(po_id)
            else:
                failed_pos.append({"po_id": po_id, "reason": "SC API rejected update"})
        except Exception as e:
            failed_pos.append({"po_id": po_id, "reason": str(e)})

    db.commit()

    return {
        "success": True, 
        "message": f"Successfully updated {len(updated_pos)} POs",
        "updated_pos": updated_pos,
        "failed_pos": failed_pos,
        "warehouse_id": str(warehouse.id),
        "warehouse_name": warehouse.name
    }


@router.patch("/{po_id}/warehouse")
def update_po_warehouse(
    po_id: str,
    warehouse_id: str = Query(..., description="UUID of the warehouse"),
    po_data: Optional[Any] = Body(None, description="Array of PO objects, list of PO IDs, or dictionary containing po_ids (used if path po_id is 'bulk')"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Update receiving warehouse for one or more purchase orders in both local DB and SellerCloud.
    
    Accepts a single PO UUID/SellerCloud PO ID or a comma-separated list of IDs in path,
    or if path po_id is 'bulk', accepts a JSON array or object in the request body (e.g. {"po_ids": ["11880", "11881"]} or [{"po_id": "11880"}]).
    """
    if current_user.role == "vendor":
        raise HTTPException(status_code=403, detail="Vendors cannot update warehouse")

    try:
        import uuid
        w_uuid = uuid.UUID(warehouse_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid warehouse_id format (must be UUID)")
        
    warehouse = db.query(models.Warehouse).filter(models.Warehouse.id == w_uuid).first()
    if not warehouse:
        raise HTTPException(status_code=404, detail=f"Warehouse '{warehouse_id}' not found")
        
    if not warehouse.sellercloud_warehouse_id:
        raise HTTPException(status_code=400, detail="Warehouse must have a SellerCloud ID to sync")

    # Extract PO identifiers from body if path po_id is 'bulk'
    if po_id.lower() == "bulk":
        if po_data is None:
            raise HTTPException(status_code=400, detail="po_data body is required when po_id path is 'bulk'")
        po_idents = []
        if isinstance(po_data, dict):
            # Check for common list keys like "po_ids", "po_id", "ids", "id"
            found_key = False
            for key in ["po_ids", "po_id", "ids", "id"]:
                val = po_data.get(key)
                if val is not None:
                    found_key = True
                    if isinstance(val, list):
                        po_idents.extend([str(item) for item in val if item])
                    else:
                        po_idents.append(str(val))
                    break
            if not found_key:
                # If no matching key is found, try to extract any list value or check if dict itself has keys
                for val in po_data.values():
                    if isinstance(val, list):
                        po_idents.extend([str(item) for item in val if item])
                        found_key = True
                        break
        elif isinstance(po_data, list):
            for item in po_data:
                if isinstance(item, dict):
                    val = item.get("po_id") or item.get("id") or item.get("sellercloud_po_id")
                    if val:
                        po_idents.append(str(val))
                elif isinstance(item, (str, int)):
                    po_idents.append(str(item))
    else:
        # Split the po_id by comma to handle multiple IDs
        po_idents = [p.strip() for p in po_id.split(",") if p.strip()]
        
    if not po_idents:
        raise HTTPException(status_code=400, detail="No purchase order IDs provided")

    from app.services.sellercloud_client import sellercloud_client
    
    updated_pos = []
    errors = []

    for po_ident in po_idents:
        po = None
        # Try to parse as integer (sellercloud_po_id)
        try:
            sc_po_id = int(po_ident)
            po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sc_po_id).first()
        except ValueError:
            # Not an integer, lookup by UUID
            try:
                po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == uuid.UUID(po_ident)).first()
            except Exception:
                pass
                
        if not po:
            errors.append(f"Purchase order '{po_ident}' not found")
            continue
            
        if not po.sellercloud_po_id:
            errors.append(f"Purchase order '{po_ident}' does not have a SellerCloud ID")
            continue

        # Update in SellerCloud
        try:
            success = sellercloud_client.update_purchase_order_warehouse(po.sellercloud_po_id, warehouse.sellercloud_warehouse_id)
            if not success:
                errors.append(f"Failed to update PO {po_ident} in SellerCloud")
                continue
        except Exception as e:
            errors.append(f"SellerCloud API error on PO {po_ident}: {str(e)}")
            continue

        # Update locally
        old_warehouse_id = str(po.warehouse_id) if po.warehouse_id else None
        po.warehouse_id = warehouse.id
        db.commit()
        db.refresh(po)
        
        changes = [{"field": "warehouse_id", "old": old_warehouse_id, "new": str(warehouse.id)}]
        log_activity(db, action="UPDATE_PO_WAREHOUSE", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"changes": changes, "warehouse_name": warehouse.name})
        
        updated_pos.append({
            "id": str(po.id),
            "sellercloud_po_id": po.sellercloud_po_id,
            "warehouse_id": str(warehouse.id),
            "warehouse_name": warehouse.name
        })

    if not updated_pos:
        raise HTTPException(status_code=400, detail={"message": "Failed to update any purchase orders", "errors": errors})

    return {
        "success": True,
        "message": f"Warehouse updated successfully for {len(updated_pos)} purchase orders",
        "purchase_orders": updated_pos,
        "errors": errors if errors else None
    }


@router.post("/{sellercloud_po_id}/sync-quantities")
def sync_po_item_quantities(
    sellercloud_po_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Fetch PO details from SellerCloud and update ONLY qty_ordered and qty_in_container in the local DB.
    """
    # 1. Fetch details from SellerCloud
    from app.services.sellercloud_client import sellercloud_client
    try:
        detail = sellercloud_client.get_purchase_order(sellercloud_po_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SellerCloud API error: {str(e)}")

    if not detail or "Items" not in detail:
        raise HTTPException(status_code=404, detail="PO items not found in SellerCloud response")

    # 2. Find local PO
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id).first()
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase Order {sellercloud_po_id} not found in local database")

    # 3. Update item quantities
    updated_items = []
    sc_items = detail.get("Items") or []
    import uuid

    for sc_item in sc_items:
        sku = sc_item.get("Sku") or sc_item.get("ProductID")
        sc_item_id = sc_item.get("ID")
        
        # Look up item locally by PO reference and SKU or SC Item ID
        po_item = db.query(models.PurchaseOrderItem).filter(
            models.PurchaseOrderItem.purchase_order_id == po.id,
            (models.PurchaseOrderItem.sku == sku) | (models.PurchaseOrderItem.sellercloud_item_id == sc_item_id)
        ).first()

        if po_item:
            old_ordered = po_item.qty_ordered
            old_container = po_item.qty_in_container
            
            new_ordered = sc_item.get("QtyOrdered", 0)
            new_container = sc_item.get("QtyInContainer") or 0
            
            po_item.qty_ordered = new_ordered
            po_item.qty_in_container = new_container
            
            updated_items.append({
                "sku": sku,
                "sellercloud_item_id": sc_item_id,
                "old_qty_ordered": old_ordered,
                "new_qty_ordered": new_ordered,
                "old_qty_in_container": old_container,
                "new_qty_in_container": new_container
            })

    db.commit()

    return {
        "success": True,
        "message": f"Successfully updated quantities for {len(updated_items)} items",
        "purchase_order_id": str(po.id),
        "sellercloud_po_id": sellercloud_po_id,
        "updated_items": updated_items
    }


@router.post("/sync", response_model=SyncResponse)
def trigger_sync(
    view_id: Optional[int] = Query(None, description="SellerCloud saved PO view ID, defaults to 25"),
    db: Session = Depends(get_db),
):
    """Pulls latest Purchase Orders (+ line items) from SellerCloud into Neon."""
    try:
        count = sync_purchase_orders(db, view_id=view_id)
        return SyncResponse(
            success=True, 
            message="Sync completed successfully", 
            records_synced=count, 
            entity_type="purchase_orders", 
            status="success"
        )
    except Exception as e:
        return SyncResponse(
            success=False, 
            message="Sync failed", 
            error=str(e), 
            entity_type="purchase_orders", 
            status="error"
        )


@router.post("/{sellercloud_po_id}/sync")
def trigger_single_po_sync(
    sellercloud_po_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Sync a specific purchase order by its SellerCloud PO ID.
    
    This will:
    1. Fetch the PO detail from SellerCloud
    2. Upsert the PO and its items into the database
    3. Update existing PO if it already exists
    
    Example: POST /api/v1/purchase-orders/12147/sync
    
    Returns:
    - sellercloud_po_id: The PO ID that was synced
    - status: success or error
    - message: Details about the sync
    """
    from app.services.sync_service import _map_po, _get_or_create_company, _get_or_create_vendor, _upsert_items, _get_or_create_warehouse
    from app.services.sellercloud_client import sellercloud_client
    from app.services.activity_service import log_activity
    
    try:
        # Fetch PO detail from SellerCloud
        detail = sellercloud_client.get_purchase_order_detail(sellercloud_po_id)
        
        if not detail:
            raise HTTPException(status_code=404, detail=f"PO {sellercloud_po_id} not found in SellerCloud")
        
        # Map the PO data
        mapped = _map_po(detail)
        
        # Get or create related entities
        purchase = detail.get("Purchase") or {}
        company_sc_id = purchase.get("CompanyId")
        vendor_sc_id = purchase.get("VendorId")
        
        company = _get_or_create_company(db, company_sc_id)
        vendor = _get_or_create_vendor(db, vendor_sc_id)
        
        warehouse_sc_id = mapped.pop("sellercloud_warehouse_id", None)
        warehouse = _get_or_create_warehouse(db, warehouse_sc_id)
        
        # Extract channel and customer info dynamically
        from app.services.sync_service import _extract_order_info_from_po_detail, _get_or_create_channel
        order_info = _extract_order_info_from_po_detail(db, detail)
        
        customer_id = order_info["customer_id"]
        channel_order_id = order_info["channel_order_id"]
        channel_id = order_info["channel_id"]
        
        # Fallback for channel info if missing from RelatedItems
        if (not customer_id or not channel_order_id) and mapped.get("purchase_title"):
            try:
                import re
                match = re.search(r"Created for Order#\s*(\d+)", mapped["purchase_title"])
                if match:
                    order_id = match.group(1)
                    order_detail = sellercloud_client.get_order_detail(order_id)
                    if order_detail:
                        if not customer_id:
                            customer_email = order_detail.get("CustomerEmail")
                            if customer_email:
                                from app.services.sync_service import _get_or_create_customer
                                customer = _get_or_create_customer(db, {"CustomerEmail": customer_email})
                                if customer:
                                    customer_id = customer.id
                        
                        order_details_block = order_detail.get("OrderDetails", {})
                        if not channel_order_id:
                            channel_order_id = order_details_block.get("OrderSourceOrderId")
                        if not channel_id:
                            from app.services.sync_service import _get_channel_name_from_order
                            channel_name = _get_channel_name_from_order(order_detail)
                            if channel_name and channel_name != "Unknown":
                                channel = _get_or_create_channel(db, channel_name)
                                if channel:
                                    channel_id = channel.id
            except Exception as e:
                print(f"Fallback order fetch failed for PO {sellercloud_po_id}: {e}")

        # Upsert the PO
        existing_po = (
            db.query(models.PurchaseOrder)
            .filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id)
            .first()
        )
        
        if existing_po:
            # Update existing PO
            for key, val in mapped.items():
                setattr(existing_po, key, val)
            existing_po.company_id = company.id if company else None
            existing_po.vendor_id = vendor.id if vendor else None
            existing_po.warehouse_id = warehouse.id if warehouse else None
            
            if customer_id:
                existing_po.customer_id = customer_id
            if channel_order_id:
                existing_po.channel_order_id = channel_order_id
            if channel_id:
                existing_po.channel_id = channel_id
                
            po_row = existing_po
        else:
            # Create new PO
            po_row = models.PurchaseOrder(
                **mapped,
                company_id=company.id if company else None,
                vendor_id=vendor.id if vendor else None,
                warehouse_id=warehouse.id if warehouse else None,
                customer_id=customer_id,
                channel_order_id=channel_order_id,
                channel_id=channel_id
            )
            db.add(po_row)
        
        db.flush()  # Get po_row.id
        
        # Upsert items
        items = detail.get("Items") or []
        _upsert_items(db, po_row.id, items)
        
        db.commit()
        
        # Log activity
        log_activity(db, action="SYNC_PO", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(sellercloud_po_id))
        
        return {
            "sellercloud_po_id": sellercloud_po_id,
            "status": "success",
            "message": f"Successfully synced PO {sellercloud_po_id}",
            "items_count": len(items)
        }
        
    except HTTPException as e:
        # Return structured error response instead of 404
        return {
            "success": False,
            "message": "PO not found",
            "error": str(e.detail)
        }
    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "message": "Error syncing PO",
            "error": str(e)
        }


@router.post("/sync-all")
def trigger_all_database_pos_full_sync(
    limit: Optional[int] = Query(None, description="Limit number of database POs to sync (for testing/batching)"),
    offset: Optional[int] = Query(0, description="Offset for database POs list"),
    po_ids: Optional[List[int]] = Query(None, description="Optional specific PO IDs to sync"),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    **FULL SYNC FOR ALL PURCHASE ORDERS IN DATABASE**
    
    Iterates over all Purchase Orders currently stored in the local database and:
    1. Fetches updated PO details, statuses, dates, amounts, and item lines from SellerCloud
    2. Updates vendor, company, warehouse, channel, and customer associations
    3. Upserts all line items while preserving container associations
    4. Recalculates PO shipment statuses
    5. Discovers and syncs all shipping containers and container-item links for each PO
    
    Parameters:
    - limit: (Optional) Max number of POs to sync
    - offset: (Optional) Starting offset
    - po_ids: (Optional) Specific PO IDs from database to sync
    """
    from app.services.sync_service import sync_all_db_purchase_orders
    from app.services.activity_service import log_activity
    
    try:
        result = sync_all_db_purchase_orders(db, limit=limit, offset=offset, po_ids=po_ids)
        
        log_activity(
            db,
            action="SYNC_ALL_DB_POS_FULL",
            user_id=current_user.id,
            entity_type="PURCHASE_ORDER",
            entity_id="ALL",
            details={
                "pos_synced": result.get("stats", {}).get("pos_synced", 0),
                "pos_failed": result.get("stats", {}).get("pos_failed", 0),
                "containers_synced": result.get("stats", {}).get("containers_synced", 0)
            }
        )
        
        return result
    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "message": "Error performing full sync for database POs",
            "error": str(e)
        }


@router.post("/{sellercloud_po_id}/sync-all")
def trigger_full_po_sync(
    sellercloud_po_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Full sync for a specific purchase order.
    1. Syncs PO details and items from SellerCloud.
    2. Syncs/resolves containers and links for all its items.
    """
    from app.services.sync_service import sync_single_po_full
    from app.services.activity_service import log_activity
    
    try:
        result = sync_single_po_full(db, sellercloud_po_id)
        
        log_activity(db, action="SYNC_PO_FULL", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(sellercloud_po_id))
        
        return {
            "success": True,
            "sellercloud_po_id": sellercloud_po_id,
            "status": "success",
            "message": f"Successfully performed full sync (PO + Containers) for PO {sellercloud_po_id}",
            "items_count": result.get("items_count", 0),
            "containers_synced": result.get("containers_synced", 0),
            "links_synced": result.get("links_synced", 0),
            "synced_container_names": result.get("synced_container_names", [])
        }
        
    except ValueError as e:
        return {
            "success": False,
            "message": "PO not found",
            "error": str(e)
        }
    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "message": "Error performing full sync",
            "error": str(e)
        }



@router.post("/sync-containers")
def trigger_all_containers_sync(
    days: int = Query(30, description="Sync containers for POs from last N days (default: 30)"),
    skip_with_containers: bool = Query(False, description="Skip POs that already have containers (set to true to speed up, false to catch deletions)"),
    limit: Optional[int] = Query(None, description="Limit number of POs to sync (for testing)"),
    db: Session = Depends(get_db)
):
    """
    **OPTIMIZED BULK CONTAINER SYNC** - Syncs containers with minimal bandwidth.
    
    This endpoint has been optimized to reduce bandwidth usage by 80-95%:
    - Only syncs POs from last N days (default: 30) instead of ALL POs
    - Skips POs that already have container data (optional)
    - Provides detailed progress tracking
    
    **Bandwidth comparison:**
    - Old full sync: ~35 MB (all POs, all time)
    - Optimized sync (30 days): ~3-5 MB
    - Optimized sync (7 days): ~1-2 MB
    
    Parameters:
    - days: Look back period (default: 30 days, set to 365 for older data)
    - skip_with_containers: Skip POs already synced (default: True, faster)
    - limit: Max POs to process, for testing (default: None = all matching POs)
    
    Examples:
    - Recent POs only: POST /api/v1/purchase-orders/sync-containers?days=7
    - Last month: POST /api/v1/purchase-orders/sync-containers?days=30
    - Re-sync all: POST /api/v1/purchase-orders/sync-containers?days=30&skip_with_containers=false
    - Test with 10 POs: POST /api/v1/purchase-orders/sync-containers?days=30&limit=10
    
    Returns detailed statistics about what was synced.
    """
    sync_service = OptimizedSyncService(db)
    result = sync_service.sync_containers_bulk_optimized(
        days=days,
        skip_with_containers=skip_with_containers,
        limit=limit
    )
    
    if result.get("success"):
        stats = result.get("stats", {})
        return {
            "success": True,
            "message": result.get("message"),
            "data": {
                "pos_checked": stats.get("pos_checked", 0),
                "pos_processed": stats.get("pos_processed", 0),
                "pos_skipped": stats.get("pos_skipped", 0),
                "containers_synced": stats.get("containers_synced", 0),
                "links_synced": stats.get("links_synced", 0),
                "days_synced": result.get("days_synced"),
                "bandwidth_saved": stats.get("bandwidth_saved"),
                "stopped_early": stats.get("stopped_early", False)
            },
            "errors": result.get("errors", [])
        }
    else:
        return {
            "success": False,
            "message": "Sync failed or partially failed",
            "error": result.get("error"),
            "errors": result.get("errors", []),
            "data": result.get("stats", {})
        }


@router.post("/{sellercloud_po_id}/sync-containers")
def trigger_container_sync(sellercloud_po_id: int, db: Session = Depends(get_db)):
    """
    Discovers and syncs shipping container data for one PO's items (container
    name, ETA, received date, and per-item quantities). Scoped to a single PO
    to keep the SellerCloud API call volume reasonable - a container fetch
    triggered here may also backfill links for OTHER already-synced POs that
    share the same consolidated container.
    """
    try:
        result = sync_containers(db, po_id=sellercloud_po_id)
        return {
            "success": True,
            "message": f"Successfully synced containers for PO {sellercloud_po_id}",
            "data": {
                "sellercloud_po_id": sellercloud_po_id,
                "containers_synced": result.get("containers_synced", 0),
                "links_synced": result.get("links_synced", 0),
            }
        }
    except Exception as e:
        db.rollback()
        return {
            "success": False,
            "message": "Error syncing PO containers",
            "error": str(e)
        }



# ============================================================================
# CSV EXPORT ENDPOINTS
# ============================================================================

@router.get("/{sellercloud_po_id}/export/csv")
def export_single_po_csv(
    sellercloud_po_id: int,
    db: Session = Depends(get_db)
):
    """
    Export a single PO with all its details to CSV.
    
    Returns a downloadable CSV file containing:
    - PO header information
    - All line items with quantities, prices
    - Container information for each item
    - Vendor details
    
    Example: GET /api/v1/purchase-orders/11880/export/csv
    """
    # Fetch PO with all related data
    po = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items)
                .joinedload(models.PurchaseOrderItem.container_links)
                .joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.channel),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments),
            joinedload(models.PurchaseOrder.comments)
                .joinedload(models.PurchaseOrderComment.user)
        )
        .filter(models.PurchaseOrder.sellercloud_po_id == sellercloud_po_id)
        .first()
    )
    
    if not po:
        raise HTTPException(status_code=404, detail=f"PO {sellercloud_po_id} not found")
    
    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write PO header information
    writer.writerow(["PURCHASE ORDER DETAILS"])
    writer.writerow(["PO ID", po.sellercloud_po_id])
    writer.writerow(["Title", po.purchase_title or ""])
    writer.writerow(["Vendor", po.vendor.name if po.vendor else ""])
    writer.writerow(["Channel", po.channel.name if po.channel else ""])
    writer.writerow(["Channel Order ID", po.channel_order_id or ""])
    writer.writerow(["Status Code", po.purchase_order_status_code or ""])
    writer.writerow(["Receiving Status", po.receiving_status_code or ""])
    writer.writerow(["Created On", po.created_on.strftime('%Y-%m-%d') if po.created_on else ""])
    writer.writerow(["Date Ordered", po.date_ordered.strftime('%Y-%m-%d') if po.date_ordered else ""])
    writer.writerow(["Expected Delivery", po.expected_delivery_date.strftime('%Y-%m-%d') if po.expected_delivery_date else ""])
    writer.writerow(["Invoice Date", po.invoice_date.strftime('%Y-%m-%d') if po.invoice_date else ""])
    lead_time = po.container_lead_time_days if po.container_lead_time_days is not None else (po.vendor.container_lead_time_days if po.vendor else "")
    writer.writerow(["Lead Time (days)", lead_time])
    writer.writerow(["Total Amount", f"{po.total_amount or 0} {po.currency or 'USD'}"])
    writer.writerow(["Notes", po.notes or ""])
    comments_str = " | ".join(
        [f"[{c.created_at.strftime('%Y-%m-%d')}] {c.user.full_name or c.user.email if c.user else 'Unknown'}: {c.comment}" for c in po.comments]
    ) if getattr(po, "comments", None) else ""
    writer.writerow(["Comments", comments_str])
    writer.writerow([])  # Empty row
    
    # Write items header
    writer.writerow(["LINE ITEMS"])
    writer.writerow([
        "Item ID", "SKU", "Product Name", "Qty Ordered", "Qty Received", 
        "Qty in Container", "Unit Price", "Cases Ordered", "Units per Case",
        "Case Price", "Expected Delivery", "Container Name", "Container ETA"
    ])
    
    # Write each item
    for item in po.items:
        # Get container info if available
        container_name = ""
        container_eta = ""
        if item.container_links:
            containers = [link.container for link in item.container_links if link.container]
            if containers:
                container_name = ", ".join([c.container_name or "" for c in containers])
                container_eta = ", ".join([
                    c.estimated_arrival_date.strftime('%Y-%m-%d') if c.estimated_arrival_date else ""
                    for c in containers
                ])
        
        writer.writerow([
            item.sellercloud_item_id or "",
            item.sku or "",
            item.product_name or "",
            item.qty_ordered or 0,
            item.qty_received or 0,
            item.qty_in_container or 0,
            item.unit_price or 0,
            item.qty_cases_ordered or 0,
            item.qty_units_per_case or 0,
            item.case_price or 0,
            item.expected_delivery_date.strftime('%Y-%m-%d') if item.expected_delivery_date else "",
            container_name,
            container_eta
        ])
    
    # Prepare response
    output.seek(0)
    filename = f"PO_{sellercloud_po_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8')),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.post("/export/csv")
def export_multiple_pos_csv(
    request_data: POExportRequest,
    db: Session = Depends(get_db)
):
    """
    Export multiple POs to a single CSV file.

    Provide a list of SellerCloud PO IDs (or internal PO UUIDs) to export, or a
    "filter_status" (invoice_delayed, delivery_delayed, lefts_items) to export a
    predefined category. Omit both "po_ids" and "filter_status" to export all
    purchase orders. Optionally provide "columns" to select/order a subset of
    columns (see PO_EXPORT_COLUMNS for valid names) - if only PO-level columns
    are selected, one row is written per PO; otherwise one row is written per
    line item.

    Example: POST /api/v1/purchase-orders/export/csv
    Body: {"po_ids": [11880, 11881, 11882], "columns": ["PO ID", "Vendor", "Total Amount"]}

    Returns a downloadable CSV file.
    """
    po_ids = request_data.po_ids

    query = db.query(models.PurchaseOrder).options(
        joinedload(models.PurchaseOrder.items)
            .joinedload(models.PurchaseOrderItem.container_links)
            .joinedload(models.PurchaseOrderItemContainer.container),
        joinedload(models.PurchaseOrder.vendor),
        joinedload(models.PurchaseOrder.customer),
        joinedload(models.PurchaseOrder.channel),
        joinedload(models.PurchaseOrder.comments),
        joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments),
        joinedload(models.PurchaseOrder.comments)
            .joinedload(models.PurchaseOrderComment.user)
    )

    if request_data.channel_id:
        try:
            query = query.filter(models.PurchaseOrder.channel_id == uuid.UUID(request_data.channel_id))
        except ValueError:
            # Fallback: if it's not a UUID, try filtering by channel_order_id
            query = query.filter(models.PurchaseOrder.channel_order_id.ilike(f"%{request_data.channel_id}%"))

    if getattr(request_data, 'channel_order_id', None):
        query = query.filter(models.PurchaseOrder.channel_order_id.ilike(f"%{request_data.channel_order_id}%"))

    if request_data.vendor_id:
        query = query.filter(models.PurchaseOrder.vendor_id == request_data.vendor_id)

    if request_data.customer_id:
        if request_data.customer_id == "00000000-0000-0000-0000-000000000000" or request_data.customer_id == "0":
            query = query.filter(models.PurchaseOrder.customer_id.is_(None))
        elif request_data.customer_id.isdigit():
            query = query.join(models.Customer, models.PurchaseOrder.customer_id == models.Customer.id).filter(
                models.Customer.sellercloud_customer_id == int(request_data.customer_id)
            )
        else:
            query = query.filter(models.PurchaseOrder.customer_id == request_data.customer_id)

    if request_data.search:
        import re
        from sqlalchemy import cast, String
        escaped_search = re.escape(request_data.search)
        search_conditions = [
            models.PurchaseOrder.purchase_title.op('~*')(rf"\y{escaped_search}"),
            models.PurchaseOrder.vendor.has(models.Vendor.name.op('~*')(rf"\y{escaped_search}")),
            models.PurchaseOrder.company.has(models.Company.name.ilike(f"{request_data.search}%")),
            models.PurchaseOrder.customer.has(models.Customer.first_name.ilike(f"{request_data.search}%")),
            models.PurchaseOrder.customer.has(models.Customer.last_name.ilike(f"{request_data.search}%")),
            models.PurchaseOrder.channel.has(models.Channel.name.ilike(f"{request_data.search}%")),
            models.PurchaseOrder.channel_order_id.ilike(f"{request_data.search}%")
        ]
        if request_data.search.isdigit():
            search_conditions.append(cast(models.PurchaseOrder.sellercloud_po_id, String).op('~*')(rf"\y{escaped_search}"))

        query = query.filter(or_(*search_conditions))

    if request_data.date_from:
        query = query.filter(models.PurchaseOrder.date_ordered >= request_data.date_from)
    if request_data.date_to:
        date_to = request_data.date_to
        # If date_to has no time component (midnight), extend it to the end of the day
        if date_to.time() == datetime.min.time():
            from datetime import time
            date_to = datetime.combine(date_to.date(), time(23, 59, 59, 999999))
        query = query.filter(models.PurchaseOrder.date_ordered <= date_to)

    if po_ids:
        sellercloud_ids = [i for i in po_ids if isinstance(i, int)]
        uuid_ids = [i for i in po_ids if isinstance(i, uuid.UUID)]
        query = query.filter(
            or_(
                models.PurchaseOrder.sellercloud_po_id.in_(sellercloud_ids) if sellercloud_ids else False,
                models.PurchaseOrder.id.in_(uuid_ids) if uuid_ids else False,
            )
        )
        pos = query.all()
    elif request_data.filter_status:
        from datetime import timezone
        cutoff_10_days = datetime.now(timezone.utc) - timedelta(days=10)
        today = datetime.now(timezone.utc).date()

        status = request_data.filter_status
        if not status or status.lower() == "all":
            pos = query.order_by(models.PurchaseOrder.created_on.desc()).all()
        elif status == "invoice_delayed":
            pos = query.filter(
                and_(
                    models.PurchaseOrder.invoice_date.is_(None),
                    models.PurchaseOrder.created_on <= cutoff_10_days
                )
            ).order_by(models.PurchaseOrder.created_on.asc()).all()
        elif status == "delivery_delayed":
            candidates = query.filter(
                and_(
                    models.PurchaseOrder.invoice_date.isnot(None),
                    models.PurchaseOrder.container_lead_time_days.isnot(None)
                )
            ).all()
            pos = [
                p for p in candidates
                if p.invoice_date and p.container_lead_time_days
                and (p.invoice_date.date() + timedelta(days=p.container_lead_time_days)) < today
            ]
        elif status == "lefts_items":
            candidates = query.join(
                models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id
            ).distinct().all()
            pos = [
                p for p in candidates
                if sum(item.qty_ordered - item.qty_received for item in p.items) > 0
            ]
        else:
            raise HTTPException(status_code=400, detail="Invalid filter_status. Must be invoice_delayed, delivery_delayed, or lefts_items")
    else:
        pos = query.all()

    if not pos:
        raise HTTPException(status_code=404, detail="No POs found with provided criteria")

    def lead_time_of(po):
        return po.container_lead_time_days if po.container_lead_time_days is not None else (po.vendor.container_lead_time_days if po.vendor else "")

    def container_info_of(item):
        containers = [link.container for link in (item.container_links or []) if link.container]
        name = ", ".join([c.container_name or "" for c in containers])
        eta = ", ".join([c.estimated_arrival_date.strftime('%Y-%m-%d') if getattr(c, 'estimated_arrival_date', None) else "" for c in containers])
        return name, eta

    # column name -> (is_item_level, extractor(po, item))
    PO_EXPORT_COLUMNS = {
        "PO ID": (False, lambda po, item: po.sellercloud_po_id),
        "PO Title": (False, lambda po, item: po.purchase_title or ""),
        "Vendor": (False, lambda po, item: po.vendor.name if po.vendor else ""),
        "Customer Name": (False, lambda po, item: f"{po.customer.first_name or ''} {po.customer.last_name or ''}".strip() if po.customer else ""),
        "Channel": (False, lambda po, item: po.channel.name if po.channel else ""),
        "Channel ID": (False, lambda po, item: po.channel_order_id or ""),
        "Channel Order ID": (False, lambda po, item: po.channel_order_id or ""),
        "Warehouse": (False, lambda po, item: po.warehouse.name if po.warehouse else ""),
        "Status Code": (False, lambda po, item: po.purchase_order_status_code or ""),
        "Receiving Status": (False, lambda po, item: po.receiving_status_code or ""),
        "Created On": (False, lambda po, item: po.created_on.strftime('%Y-%m-%d') if po.created_on else ""),
        "Date Ordered": (False, lambda po, item: po.date_ordered.strftime('%Y-%m-%d') if po.date_ordered else ""),
        "Expected Delivery": (False, lambda po, item: po.expected_delivery_date.strftime('%Y-%m-%d') if po.expected_delivery_date else ""),
        "Invoice Date": (False, lambda po, item: po.invoice_date.strftime('%Y-%m-%d') if po.invoice_date else ""),
        "Lead Time (days)": (False, lambda po, item: lead_time_of(po)),
        "Total Amount": (False, lambda po, item: po.total_amount or 0),
        "Currency": (False, lambda po, item: po.currency or "USD"),
        "Item ID": (True, lambda po, item: item.sellercloud_item_id or ""),
        "SKU": (True, lambda po, item: item.sku or ""),
        "Product Name": (True, lambda po, item: item.product_name or ""),
        "Qty Ordered": (True, lambda po, item: item.qty_ordered or 0),
        "Qty Received": (True, lambda po, item: item.qty_received or 0),
        "Qty in Container": (True, lambda po, item: item.qty_in_container or 0),
        "Unit Price": (True, lambda po, item: item.unit_price or 0),
        "Cases Ordered": (True, lambda po, item: item.qty_cases_ordered or 0),
        "Units per Case": (True, lambda po, item: item.qty_units_per_case or 0),
        "Case Price": (True, lambda po, item: item.case_price or 0),
        "Item Expected Delivery": (True, lambda po, item: item.expected_delivery_date.strftime('%Y-%m-%d') if item.expected_delivery_date else ""),
        "Container Name": (True, lambda po, item: container_info_of(item)[0]),
        "Container ETA": (True, lambda po, item: container_info_of(item)[1]),
        "Notes": (False, lambda po, item: po.notes or ""),
        "Comments": (False, lambda po, item: " | ".join(
            [f"[{c.created_at.strftime('%Y-%m-%d %H:%M')}] {c.user.full_name or c.user.email if c.user else 'Unknown'}: {c.comment}" for c in po.comments]
        ) if getattr(po, "comments", None) else ""),
        "Item Comments": (True, lambda po, item: " | ".join(
            [f"[{c.created_at.strftime('%Y-%m-%d %H:%M')}] {c.user.full_name or c.user.email if c.user else 'Unknown'}: {c.comment}" for c in item.comments]
        ) if item and getattr(item, "comments", None) else ""),
    }

    if request_data.columns:
        unknown = [c for c in request_data.columns if c not in PO_EXPORT_COLUMNS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown column(s): {unknown}. Valid columns: {list(PO_EXPORT_COLUMNS.keys())}")
        selected_columns = request_data.columns
    else:
        selected_columns = list(PO_EXPORT_COLUMNS.keys())

    needs_item_rows = any(PO_EXPORT_COLUMNS[c][0] for c in selected_columns)

    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(selected_columns)

    for po in pos:
        if not po.items or not needs_item_rows:
            # Only output one row per PO if no item-level data is requested (or if PO has no items)
            row = [
                "" if PO_EXPORT_COLUMNS[c][0] else PO_EXPORT_COLUMNS[c][1](po, None)
                for c in selected_columns
            ]
            writer.writerow(row)
        else:
            seen_rows = set()
            first_row_written = False
            for item in po.items:
                row = [PO_EXPORT_COLUMNS[c][1](po, item) for c in selected_columns]

                row_tuple = tuple(row)
                if row_tuple not in seen_rows:
                    seen_rows.add(row_tuple)

                    # Blank out Notes for subsequent rows to avoid duplication
                    if first_row_written and "Notes" in selected_columns:
                        row[selected_columns.index("Notes")] = ""

                    writer.writerow(row)
                    first_row_written = True

    # Prepare response
    output.seek(0)
    filename = f"POs_{len(pos)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=purchase_orders.csv"}
    )


# ============================================================================
# OPTIMIZED SYNC ENDPOINTS (Bandwidth Efficient)
# ============================================================================

@router.post("/sync/optimized")
def sync_pos_optimized(
    request: Request,
    days: int = Query(7, description="Sync POs modified in last N days"),
    batch_size: int = Query(25, description="Number of POs per API call"),
    view_id: Optional[int] = Query(None, description="SellerCloud saved view ID (default: 25)"),
    db: Session = Depends(get_db)
):
    """
    **OPTIMIZED SYNC** - Syncs only recently changed POs.
    
    This endpoint reduces bandwidth by:
    - Only syncing POs created in last N days (default: 7)
    - Using smaller batch sizes
    - Skipping unchanged records
    - Smart change detection
    
    **Bandwidth comparison:**
    - Full sync: ~35 MB
    - Optimized sync (7 days): ~3 MB
    
    Parameters:
    - days: Look back period (default: 7 days)
    - batch_size: POs per API call (default: 25, smaller = less memory)
    - view_id: SellerCloud saved view ID (default: 25)
    
    Returns detailed statistics about what was synced.
    """
    # Extract 'days' parameter case-insensitively
    days_val = days
    for k, v in request.query_params.items():
        if k.lower() == "days":
            try:
                days_val = int(v)
                break
            except ValueError:
                pass

    sync_service = OptimizedSyncService(db)
    result = sync_service.sync_recent_pos(
        days=days_val, 
        batch_size=batch_size, 
        view_id=view_id
    )
    
    if result.get("success"):
        return {
            "success": True,
            "message": result.get("message"),
            "data": result.get("stats", {})
        }
    else:
        return {
            "success": False,
            "message": "Sync failed or partially failed",
            "error": result.get("error"),
            "errors": result.get("errors", []),
            "data": result.get("stats", {})
        }


@router.post("/sync/cleanup-deleted")
def cleanup_deleted_pos(db: Session = Depends(get_db)):
    """
    **DEEP CLEANUP** - Removes Hard-Deleted POs and Empty Containers.
    
    Checks every PO in the local database against SellerCloud to find any that 
    have been completely deleted from SellerCloud. If a PO is deleted, it is 
    removed from the CRM along with its line items. Any shipping containers 
    that become completely empty as a result are also deleted.
    
    This is a slower operation and should only be run periodically (e.g. weekly).
    """
    sync_service = OptimizedSyncService(db)
    result = sync_service.cleanup_deleted_pos()
    
    if result.get("success"):
        return {
            "success": True,
            "message": result.get("message"),
            "data": result.get("stats", {})
        }
    else:
        return {
            "success": False,
            "message": "Cleanup failed",
            "error": result.get("error"),
            "data": result.get("stats", {})
        }


@router.post("/sync/containers-selective")
def sync_containers_optimized(
    po_ids: list[int] = Query(None, description="Specific PO IDs to sync containers for"),
    db: Session = Depends(get_db)
):
    """
    **OPTIMIZED SELECTIVE CONTAINER SYNC** - Syncs containers only for specific POs.
    
    Instead of syncing all containers:
    - Provide specific PO IDs to sync (comma-separated)
    - OR automatically syncs containers for POs from last 30 days
    
    This reduces bandwidth by only fetching relevant containers.
    
    Examples:
    - Specific POs: POST /api/v1/purchase-orders/sync/containers-selective?po_ids=11880&po_ids=11881
    - Recent POs (auto): POST /api/v1/purchase-orders/sync/containers-selective
    
    Returns detailed statistics about what was synced.
    """
    sync_service = OptimizedSyncService(db)
    result = sync_service.sync_containers_selective(po_ids=po_ids)
    
    if result.get("success"):
        stats = result.get("stats", {})
        return {
            "success": True,
            "message": result.get("message"),
            "data": {
                "pos_processed": stats.get("pos_processed", 0),
                "containers_synced": stats.get("containers_synced", 0),
                "links_synced": stats.get("links_synced", 0),
            }
        }
    else:
        return {
            "success": False,
            "message": "Sync failed or partially failed",
            "error": result.get("error"),
            "errors": result.get("errors", []),
            "data": result.get("stats", {})
        }


@router.get("/sync/recommendations")
def get_sync_settings_recommendations(db: Session = Depends(get_db)):
    """
    Get intelligent sync recommendations based on your data.
    
    Analyzes:
    - Total number of POs
    - When last PO was created
    - Recommended sync frequency
    - Estimated bandwidth per sync
    
    Use this to determine optimal sync settings for your database.
    """
    recommendations = get_sync_recommendations(db)
    
    return {
        "recommendations": recommendations,
        "available_sync_endpoints": {
            "optimized_sync": {
                "endpoint": "POST /api/v1/purchase-orders/sync/optimized",
                "description": "Sync recent changes only (recommended)",
                "bandwidth": "Low (~3 MB for 7 days)"
            },
            "full_sync": {
                "endpoint": "POST /api/v1/purchase-orders/sync",
                "description": "Full sync of all POs",
                "bandwidth": "High (~35 MB+)"
            },
            "selective_containers": {
                "endpoint": "POST /api/v1/purchase-orders/sync/containers-selective",
                "description": "Sync containers for specific POs only",
                "bandwidth": "Low to Medium"
            }
        }
    }


@router.post("/sync/customers")
def trigger_sync_customers(db: Session = Depends(get_db)):
    """
    Sync all customers from SellerCloud to local database.
    """
    from app.services.sync_service import sync_customers
    try:
        count = sync_customers(db)
        return {"success": True, "message": f"Successfully synced {count} customers."}
    except Exception as e:
        return {"success": False, "error": str(e)}



