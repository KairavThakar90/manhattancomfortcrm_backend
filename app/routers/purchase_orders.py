from typing import Optional
from datetime import datetime, timedelta
import csv
import io

from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import PurchaseOrderOut, PaginatedResponse, SyncResponse, POExportRequest, POCommentCreate, POCommentOut, POCommentUpdate, POItemCommentCreate, POItemCommentOut, POStatusUpdate
from app.services.email_service import send_tag_notification
from app.services.sync_service import sync_purchase_orders, sync_containers
from app.services.optimized_sync_service import OptimizedSyncService, get_sync_recommendations
from app.services.activity_service import log_activity

router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"], dependencies=[Depends(get_current_user)])


async def process_comment_tags(db, tagged_user_ids, commenter_name, link, background_tasks, is_edit=False, section="Purchase Orders", po_number=None, sku=None, comment_text=""):
    if not tagged_user_ids:
        return
    import app.models as models
    users = db.query(models.User).filter(models.User.id.in_(tagged_user_ids)).all()
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
            comment_text=comment_text
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


@router.get("")
def list_purchase_orders(
    page: Optional[int] = Query(None, ge=1, description="Page number. Leave empty for all."),
    page_size: Optional[int] = Query(None, ge=1, description="Items per page. Leave empty for all."),
    status_code: Optional[int] = Query(None, description="Raw SellerCloud PurchaseOrderStatus code"),
    vendor_id: Optional[str] = None,
    sort_by: Optional[str] = Query(None, description="Field to sort by: created_on, date_ordered, invoice_date, expected_delivery_date, total_amount"),
    sort_order: Optional[str] = Query("desc", description="Sort order: asc or desc"),
    search: Optional[str] = Query(None, description="Search by PO number, order title, or vendor name"),
    date_from: Optional[datetime] = Query(None, description="Filter POs ordered on or after this date"),
    date_to: Optional[datetime] = Query(None, description="Filter POs ordered on or before this date"),
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
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
    )
    if status_code is not None:
        q = q.filter(models.PurchaseOrder.purchase_order_status_code == status_code)
    
    if current_user.role == "vendor":
        q = q.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
    elif vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
        
    if search:
        search_term = f"%{search}%"
        search_conditions = [
            models.PurchaseOrder.purchase_title.ilike(search_term),
            models.PurchaseOrder.vendor.has(models.Vendor.name.ilike(search_term))
        ]
        if search.isdigit():
            search_conditions.append(models.PurchaseOrder.sellercloud_po_id == int(search))
            
        q = q.filter(or_(*search_conditions))
        
    if date_from:
        q = q.filter(models.PurchaseOrder.date_ordered >= date_from)
    if date_to:
        # If date_to has no time component (midnight), extend it to the end of the day
        if date_to.time() == datetime.min.time():
            from datetime import time
            date_to = datetime.combine(date_to.date(), time(23, 59, 59, 999999))
        q = q.filter(models.PurchaseOrder.date_ordered <= date_to)

    # Apply sorting
    sort_field_map = {
        "created_on": models.PurchaseOrder.created_on,
        "date_ordered": models.PurchaseOrder.date_ordered,
        "invoice_date": models.PurchaseOrder.invoice_date,
        "expected_delivery_date": models.PurchaseOrder.expected_delivery_date,
        "total_amount": models.PurchaseOrder.total_amount,
    }
    
    sort_field = sort_field_map.get(sort_by, models.PurchaseOrder.date_ordered)
    
    if sort_order and sort_order.lower() == "asc":
        q = q.order_by(sort_field.asc())
    else:
        q = q.order_by(sort_field.desc())

    total = q.count()
    
    if page and page_size:
        rows = q.offset((page - 1) * page_size).limit(page_size).all()
    else:
        rows = q.all()
    
    # Convert to Pydantic models BEFORE converting to dicts
    # This ensures model_validate can access container_links
    po_models = [PurchaseOrderOut.model_validate(r) for r in rows]
    
    # Now convert to dicts
    results = [po.model_dump(mode='python', exclude={'items', 'comments'}) for po in po_models]
    
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
    import uuid
    try:
        po_uuid = uuid.UUID(po_id)
        filter_clause = models.PurchaseOrder.id == po_uuid
    except ValueError:
        if po_id.isdigit():
            filter_clause = models.PurchaseOrder.sellercloud_po_id == int(po_id)
        else:
            raise HTTPException(status_code=400, detail="Invalid PO ID format. Must be a UUID or SellerCloud integer ID.")

    po = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.container_links).joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments),
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
            
    return PurchaseOrderOut.model_validate(po)


@router.post("/{po_id}/comments", response_model=POCommentOut)
async def add_po_comment(
    po_id: str,
    comment_data: POCommentCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    import uuid
    from app.config import settings
    try:
        po_uuid = uuid.UUID(po_id)
        filter_clause = models.PurchaseOrder.id == po_uuid
    except ValueError:
        if po_id.isdigit():
            filter_clause = models.PurchaseOrder.sellercloud_po_id == int(po_id)
        else:
            raise HTTPException(status_code=400, detail="Invalid PO ID format. Must be a UUID or SellerCloud integer ID.")

    po = db.query(models.PurchaseOrder).filter(filter_clause).first()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
        
    if current_user.role == "vendor":
        if str(po.vendor_id) != str(current_user.vendor_id):
            raise HTTPException(status_code=403, detail="Not authorized to comment on this PO")
            
    new_comment = models.PurchaseOrderComment(
        purchase_order_id=po.id,
        user_id=current_user.id,
        comment=comment_data.comment,
        parent_id=comment_data.parent_id
    )
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment)
    new_comment.user_name = current_user.full_name or current_user.email
    
    # Process Tags
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?comment_id={new_comment.id}"
    await process_comment_tags(
        db=db,
        tagged_user_ids=comment_data.tagged_user_ids,
        commenter_name=new_comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=False,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=None,
        comment_text=new_comment.comment
    )
    
    log_activity(db, action="ADD_PO_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"comment_id": str(new_comment.id)})
    return new_comment

@router.put("/comments/{comment_id}", response_model=POCommentOut)
async def update_po_comment(
    comment_id: str,
    comment_data: POCommentUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    from app.config import settings
    comment = db.query(models.PurchaseOrderComment).filter(models.PurchaseOrderComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")
        
    comment.comment = comment_data.comment
    comment.is_edited = True
    db.commit()
    db.refresh(comment)
    comment.user_name = current_user.full_name or current_user.email
    
    # Process Tags
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == comment.purchase_order_id).first()
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?comment_id={comment.id}" if po else ""
    await process_comment_tags(
        db=db,
        tagged_user_ids=comment_data.tagged_user_ids,
        commenter_name=comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=True,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=None,
        comment_text=comment.comment
    )
    
    log_activity(db, action="UPDATE_PO_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id) if po else None, details={"comment_id": str(comment.id)})
    return comment

@router.post("/items/{item_id}/comments", response_model=POItemCommentOut)
async def add_po_item_comment(
    item_id: str,
    comment_data: POItemCommentCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    from app.config import settings
    item = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
        
    new_comment = models.PurchaseOrderItemComment(
        purchase_order_item_id=item.id,
        user_id=current_user.id,
        comment=comment_data.comment,
        parent_id=comment_data.parent_id
    )
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment)
    new_comment.user_name = current_user.full_name or current_user.email
    
    # Process Tags
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == item.purchase_order_id).first() if item else None
    link = f"{settings.FRONTEND_ORIGIN}/purchase-orders/{po.sellercloud_po_id}?item_id={item.sellercloud_item_id}&comment_id={new_comment.id}" if po else ""
    await process_comment_tags(
        db=db,
        tagged_user_ids=comment_data.tagged_user_ids,
        commenter_name=new_comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=False,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=item.sku if item else None,
        comment_text=new_comment.comment
    )
    
    log_activity(db, action="ADD_PO_ITEM_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER_ITEM", entity_id=str(item.id), details={"comment_id": str(new_comment.id)})
    return new_comment

@router.put("/items/comments/{comment_id}", response_model=POItemCommentOut)
async def update_po_item_comment(
    comment_id: str,
    comment_data: POCommentUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    from app.config import settings
    comment = db.query(models.PurchaseOrderItemComment).filter(models.PurchaseOrderItemComment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this comment")
        
    comment.comment = comment_data.comment
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
        tagged_user_ids=comment_data.tagged_user_ids,
        commenter_name=comment.user_name,
        link=link,
        background_tasks=background_tasks,
        is_edit=True,
        section="Purchase Orders",
        po_number=str(po.sellercloud_po_id) if po else None,
        sku=item.sku if item else None,
        comment_text=comment.comment
    )
    
    log_activity(db, action="UPDATE_PO_ITEM_COMMENT", user_id=current_user.id, entity_type="PURCHASE_ORDER_ITEM", entity_id=str(item.id) if item else None, details={"comment_id": str(comment.id)})
    return comment


@router.get("/filters/all")
def get_filtered_pos(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    filter_type: Optional[str] = Query(None, description="Filter type: new_without_invoice, invoice_delayed, delivery_overdue, remaining_items. If not provided, returns all 4 categories."),
    vendor_id: Optional[str] = Query(None, description="Filter by vendor UUID"),
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
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
            )
        )
        
        # Apply vendor filter if provided
        if current_user.role == "vendor":
            base_q = base_q.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
        elif vendor_id:
            base_q = base_q.filter(models.PurchaseOrder.vendor_id == vendor_id)
        
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
            total_remaining = sum(item.qty_ordered - item.qty_received for item in po.items)
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
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments)
        )
    )
    
    # Apply vendor filter if provided
    if current_user.role == "vendor":
        q = q.filter(models.PurchaseOrder.vendor_id == current_user.vendor_id)
    elif vendor_id:
        q = q.filter(models.PurchaseOrder.vendor_id == vendor_id)
    
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
        po = db.query(models.PurchaseOrder).options(joinedload(models.PurchaseOrder.vendor)).filter(models.PurchaseOrder.sellercloud_po_id == sc_po_id).first()
    except ValueError:
        try:
            po = db.query(models.PurchaseOrder).options(joinedload(models.PurchaseOrder.vendor)).filter(models.PurchaseOrder.id == po_id).first()
        except Exception:
            pass
            
    if not po:
        raise HTTPException(status_code=404, detail=f"Purchase order '{po_id}' not found")
        
    if current_user.role == "vendor":
        if str(po.vendor_id) != str(current_user.vendor_id):
            raise HTTPException(status_code=403, detail="Not authorized to update this PO")
            
    changed = False
    old_status = po.status
    
    if status_data.status is not None:
        po.status = status_data.status
        changed = True
        
    if changed:
        db.commit()
        db.refresh(po)
        log_activity(db, action="UPDATE_PO_STATUS", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"status": po.status})
        
        # Send email notification if vendor made the change
        if current_user.role == "vendor":
            from app.services.email_service import send_po_status_update_email
            vendor_name = po.vendor.name if po.vendor else "Unknown Vendor"
            background_tasks.add_task(
                send_po_status_update_email,
                db=db,
                po_number=str(po.sellercloud_po_id),
                old_status=old_status,
                new_status=po.status,
                vendor_name=vendor_name
            )
            
    return po

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
    
    po.container_lead_time_days = container_lead_time_days
    db.commit()
    db.refresh(po)
    
    log_activity(db, action="UPDATE_PO_LEAD_TIME", user_id=current_user.id, entity_type="PURCHASE_ORDER", entity_id=str(po.id), details={"lead_time": container_lead_time_days})
    
    return {
        "message": "Lead time updated successfully",
        "po_id": str(po.id),
        "sellercloud_po_id": po.sellercloud_po_id,
        "container_lead_time_days": po.container_lead_time_days
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
            po_row = existing_po
            
            # Delete existing items to re-create them
            db.query(models.PurchaseOrderItem).filter(
                models.PurchaseOrderItem.purchase_order_id == existing_po.id
            ).delete()
        else:
            # Create new PO
            po_row = models.PurchaseOrder(
                **mapped,
                company_id=company.id if company else None,
                vendor_id=vendor.id if vendor else None,
                warehouse_id=warehouse.id if warehouse else None
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
    writer.writerow(["Status Code", po.purchase_order_status_code or ""])
    writer.writerow(["Receiving Status", po.receiving_status_code or ""])
    writer.writerow(["Created On", po.created_on.isoformat() if po.created_on else ""])
    writer.writerow(["Date Ordered", po.date_ordered.isoformat() if po.date_ordered else ""])
    writer.writerow(["Expected Delivery", po.expected_delivery_date.isoformat() if po.expected_delivery_date else ""])
    writer.writerow(["Invoice Date", po.invoice_date.isoformat() if po.invoice_date else ""])
    lead_time = po.container_lead_time_days if po.container_lead_time_days is not None else (po.vendor.container_lead_time_days if po.vendor else "")
    writer.writerow(["Lead Time (days)", lead_time])
    writer.writerow(["Total Amount", f"{po.total_amount or 0} {po.currency or 'USD'}"])
    writer.writerow(["Notes", po.notes or ""])
    comments_str = " | ".join(
        [f"[{c.created_at.strftime('%Y-%m-%d %H:%M')}] {c.user.full_name or c.user.email if c.user else 'Unknown'}: {c.comment}" for c in po.comments]
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
                    c.estimated_arrival_date.isoformat() if c.estimated_arrival_date else ""
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
            item.expected_delivery_date.isoformat() if item.expected_delivery_date else "",
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
    Export POs to a single CSV file.
    Can be filtered by a list of PO IDs or by a specific filter status.
    Columns can be customized.
    """
    from datetime import timezone, timedelta
    from sqlalchemy import and_
    cutoff_10_days = datetime.now(timezone.utc) - timedelta(days=10)
    today = datetime.now(timezone.utc).date()

    base_q = (
        db.query(models.PurchaseOrder)
        .options(
            joinedload(models.PurchaseOrder.items)
                .joinedload(models.PurchaseOrderItem.container_links)
                .joinedload(models.PurchaseOrderItemContainer.container),
            joinedload(models.PurchaseOrder.vendor),
            joinedload(models.PurchaseOrder.comments),
            joinedload(models.PurchaseOrder.items).joinedload(models.PurchaseOrderItem.comments),
            joinedload(models.PurchaseOrder.comments)
                .joinedload(models.PurchaseOrderComment.user)
        )
    )

    if request_data.po_ids:
        pos = base_q.filter(models.PurchaseOrder.sellercloud_po_id.in_(request_data.po_ids)).all()
    else:
        status = request_data.filter_status
        if not status or status.lower() == 'all':
            pos = base_q.order_by(models.PurchaseOrder.created_on.desc()).all()
        elif status == 'invoice_delayed':
            pos = base_q.filter(
                and_(
                    models.PurchaseOrder.invoice_date.is_(None),
                    models.PurchaseOrder.created_on <= cutoff_10_days
                )
            ).order_by(models.PurchaseOrder.created_on.asc()).all()
        elif status == 'delivery_delayed':
            q = base_q.filter(
                and_(
                    models.PurchaseOrder.invoice_date.isnot(None),
                    models.PurchaseOrder.container_lead_time_days.isnot(None)
                )
            ).all()
            pos = [p for p in q if p.invoice_date and p.container_lead_time_days and (p.invoice_date.date() + timedelta(days=p.container_lead_time_days)) < today]
        elif status == 'lefts_items':
            q = base_q.join(models.PurchaseOrderItem, models.PurchaseOrder.id == models.PurchaseOrderItem.purchase_order_id).distinct().all()
            pos = []
            for po in q:
                po_model = PurchaseOrderOut.model_validate(po)
                if po_model.total_qty_remaining and po_model.total_qty_remaining > 0:
                    pos.append(po)
        else:
            raise HTTPException(status_code=400, detail="Invalid filter_status")

    if not pos:
        raise HTTPException(status_code=404, detail="No POs found")

    output = io.StringIO()
    writer = csv.writer(output)

    # Column mapping logic
    def get_container_info(item):
        if not getattr(item, 'container_links', None):
            return "", ""
        containers = [link.container for link in item.container_links if link.container]
        if not containers:
            return "", ""
        name = ", ".join([c.container_name or "" for c in containers])
        eta = ", ".join([c.estimated_arrival_date.isoformat() if getattr(c, 'estimated_arrival_date', None) else "" for c in containers])
        return name, eta

    column_map = {
        "PO ID": lambda p, i, c_name, c_eta: p.sellercloud_po_id,
        "PO Title": lambda p, i, c_name, c_eta: p.purchase_title or "",
        "Vendor": lambda p, i, c_name, c_eta: p.vendor.name if p.vendor else "",
        "Status Code": lambda p, i, c_name, c_eta: p.purchase_order_status_code or "",
        "Receiving Status": lambda p, i, c_name, c_eta: p.receiving_status_code or "",
        "Created On": lambda p, i, c_name, c_eta: p.created_on.isoformat() if p.created_on else "",
        "Date Ordered": lambda p, i, c_name, c_eta: p.date_ordered.isoformat() if p.date_ordered else "",
        "Expected Delivery": lambda p, i, c_name, c_eta: p.expected_delivery_date.isoformat() if p.expected_delivery_date else "",
        "Invoice Date": lambda p, i, c_name, c_eta: p.invoice_date.isoformat() if p.invoice_date else "",
        "Lead Time (days)": lambda p, i, c_name, c_eta: p.container_lead_time_days if p.container_lead_time_days is not None else (p.vendor.container_lead_time_days if p.vendor else ""),
        "Total Amount": lambda p, i, c_name, c_eta: p.total_amount or 0,
        "Currency": lambda p, i, c_name, c_eta: p.currency or "USD",
        "Item ID": lambda p, i, c_name, c_eta: i.sellercloud_item_id or "" if i else "",
        "SKU": lambda p, i, c_name, c_eta: i.sku or "" if i else "",
        "Product Name": lambda p, i, c_name, c_eta: i.product_name or "" if i else "",
        "Qty Ordered": lambda p, i, c_name, c_eta: i.qty_ordered or 0 if i else "",
        "Qty Received": lambda p, i, c_name, c_eta: i.qty_received or 0 if i else "",
        "Qty in Container": lambda p, i, c_name, c_eta: i.qty_in_container or 0 if i else "",
        "Unit Price": lambda p, i, c_name, c_eta: i.unit_price or 0 if i else "",
        "Cases Ordered": lambda p, i, c_name, c_eta: i.qty_cases_ordered or 0 if i else "",
        "Units per Case": lambda p, i, c_name, c_eta: i.qty_units_per_case or 0 if i else "",
        "Case Price": lambda p, i, c_name, c_eta: i.case_price or 0 if i else "",
        "Item Expected Delivery": lambda p, i, c_name, c_eta: i.expected_delivery_date.isoformat() if i and i.expected_delivery_date else "",
        "Container Name": lambda p, i, c_name, c_eta: c_name,
        "Container ETA": lambda p, i, c_name, c_eta: c_eta,
        "Notes": lambda p, i, c_name, c_eta: p.notes or "",
        "Comments": lambda p, i, c_name, c_eta: " | ".join(
            [f"[{c.created_at.strftime('%Y-%m-%d %H:%M')}] {c.user.full_name or c.user.email if c.user else 'Unknown'}: {c.comment}" for c in p.comments]
        ) if getattr(p, "comments", None) else ""
    }

    all_cols = list(column_map.keys())
    selected_cols = request_data.columns if request_data.columns else all_cols
    
    # Filter out invalid columns
    selected_cols = [c for c in selected_cols if c in column_map]
    
    writer.writerow(selected_cols)
    
    item_specific_cols = {
        "Item ID", "SKU", "Product Name", "Qty Ordered", "Qty Received", 
        "Qty in Container", "Unit Price", "Cases Ordered", "Units per Case", 
        "Case Price", "Item Expected Delivery", "Container Name", "Container ETA"
    }
    
    requires_items = any(col in item_specific_cols for col in selected_cols)
    
    for po in pos:
        if not po.items or not requires_items:
            # Only output one row per PO if no item-level data is requested (or if PO has no items)
            row = [column_map[col](po, None, "", "") for col in selected_cols]
            writer.writerow(row)
        else:
            for idx, item in enumerate(po.items):
                c_name, c_eta = get_container_info(item)
                row = [column_map[col](po, item, c_name, c_eta) for col in selected_cols]
                
                # Blank out Notes for subsequent items to avoid duplication
                if idx > 0 and "Notes" in selected_cols:
                    notes_idx = selected_cols.index("Notes")
                    row[notes_idx] = ""
                    
                writer.writerow(row)

    output.seek(0)
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
    sync_service = OptimizedSyncService(db)
    result = sync_service.sync_recent_pos(days=days, batch_size=batch_size, view_id=view_id)
    
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
