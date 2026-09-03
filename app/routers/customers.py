from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import CustomerOut, PaginatedResponse

router = APIRouter(prefix="/customers", tags=["Customers"], dependencies=[Depends(get_current_user)])


from typing import Optional

@router.get("", response_model=PaginatedResponse)
def list_customers(
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=200),
    company_id: str | None = None,
    db: Session = Depends(get_db),
):
    from sqlalchemy import func

    subq = db.query(
        models.PurchaseOrderItem.purchase_order_id,
        func.sum(models.PurchaseOrderItem.qty_ordered).label('tot_ord'),
        func.sum(models.PurchaseOrderItem.qty_received).label('tot_rec')
    ).group_by(models.PurchaseOrderItem.purchase_order_id).subquery()

    open_pos_subq = db.query(models.PurchaseOrder.id, models.PurchaseOrder.customer_id).outerjoin(
        subq, models.PurchaseOrder.id == subq.c.purchase_order_id
    ).filter(
        (func.coalesce(subq.c.tot_rec, 0) < func.coalesce(subq.c.tot_ord, 0)) | 
        (func.coalesce(subq.c.tot_ord, 0) == 0)
    ).subquery()

    q = db.query(
        models.Customer,
        func.count(open_pos_subq.c.id).label('po_count')
    ).outerjoin(
        open_pos_subq, 
        models.Customer.id == open_pos_subq.c.customer_id
    )
    
    if company_id:
        q = q.filter(models.Customer.company_id == company_id)
        
    q = q.group_by(models.Customer.id)
    
    total = q.count()
    if page and page_size:
        rows = q.order_by(models.Customer.last_name).offset((page - 1) * page_size).limit(page_size).all()
    else:
        rows = q.order_by(models.Customer.last_name).all()
    
    results = []
    
    # Add a virtual "Manhattan Comfort" customer for POs with NO customer assigned
    if not page or page == 1:
        unassigned_po_count = db.query(open_pos_subq).filter(
            open_pos_subq.c.customer_id.is_(None)
        ).count()
        if unassigned_po_count > 0:
            results.append({
                "id": "00000000-0000-0000-0000-000000000000",
                "sellercloud_customer_id": 0,
                "first_name": "Manhattan",
                "last_name": "Manhattan Comfort",
                "email": "",
                "phone": "",
                "company_id": None,
                "is_active": True,
                "po_count": unassigned_po_count
            })
            total += 1
            
    for customer, po_count in rows:
        cust_dict = CustomerOut.model_validate(customer).model_dump(mode='python')
        cust_dict['po_count'] = po_count
        results.append(cust_dict)
        
    return PaginatedResponse(
        total=total,
        page=page if page else 1,
        page_size=page_size if page_size else total,
        results=results,
    )


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, db: Session = Depends(get_db)):
    return db.query(models.Customer).filter(models.Customer.id == customer_id).first()
