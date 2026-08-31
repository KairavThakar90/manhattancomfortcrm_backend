from sqlalchemy.orm import Session
from app import models

def recalculate_po_shipment_status(db: Session, po_id: str):
    """
    Recalculates the shipment_status of a PO based on the quantity of items
    in containers vs ordered quantity.
    
    If total_in_container > 0 and < total_ordered => Partially Shipped
    If total_in_container >= total_ordered => Shipped
    Otherwise leaves as is (e.g. Not Planned, Planned)
    """
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id).first()
    if not po:
        return
        
    items = db.query(models.PurchaseOrderItem).filter(models.PurchaseOrderItem.purchase_order_id == po.id).all()
    if not items:
        return
        
    total_ordered = sum(item.qty_ordered for item in items if item.qty_ordered)
    total_in_container = sum(item.qty_in_container for item in items if item.qty_in_container)
    
    manual_statuses = {"delayed", "in_production", "planned", "not_planned", "completed", "not_started"}
    current_status_lower = po.status.lower() if po.status else ""
    
    is_fully_shipped = total_in_container >= total_ordered if total_ordered > 0 else False
    
    if is_fully_shipped:
        po.status = "SHIPPED"
    else:
        if current_status_lower not in manual_statuses:
            if total_in_container > 0:
                po.status = "PARTIALLY_SHIPPED"
            else:
                po.status = None
            
    db.commit()
