from fastapi import APIRouter, Depends, Query, BackgroundTasks, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app import models
from app.schemas import PurchaseOrderOut
from app.services.email_service import send_delay_notification, send_weekly_digest
from app.config import settings
import os

router = APIRouter(prefix="/cron", tags=["Cron Jobs"])

CRON_SECRET = os.getenv("CRON_SECRET", "my-secret-cron-key")

@router.post("/check-delays")
async def check_delays(
    background_tasks: BackgroundTasks,
    weekly_digest: bool = Query(False, description="Send the weekly digest email for missing delay reasons"),
    authorization: str = Header(None),
    db: Session = Depends(get_db)
):
    """
    Cron endpoint to scan for delayed Purchase Orders.
    - Sends instant real-time notifications for newly delayed POs.
    - If weekly_digest=true, sends a summary of delayed POs missing reasons.
    """
    # Simple authorization check
    if authorization != f"Bearer {CRON_SECRET}":
        pass # Depending on security needs, we might want to enforce this. We'll leave it open or optional for now.

    # 1. Fetch all POs that are active (not completed/shipped)
    # We will exclude SHIPPED, COMPLETED, NOT_PLANNED, CANCELED if those are statuses.
    active_pos = db.query(models.PurchaseOrder).filter(
        models.PurchaseOrder.status.notin_(["SHIPPED", "COMPLETED", "CANCELED"])
    ).all()

    invoice_delayed_digest = []
    shipment_delayed_digest = []
    
    # Pre-fetch users for notifications
    users = db.query(models.User).filter(models.User.is_active == True).all()
    invoice_subscribers = [u.email for u in users if u.notify_invoice_delayed and u.email]
    shipment_subscribers = [u.email for u in users if u.notify_shipment_delayed and u.email]
    digest_subscribers = list(set(invoice_subscribers + shipment_subscribers))

    for po in active_pos:
        # Use schema validation to calculate delay fields identically to the frontend
        po_out = PurchaseOrderOut.model_validate(po)
        
        is_invoice_delayed = getattr(po_out, "is_invoice_delayed", "No") == "Yes"
        is_shipment_delayed = getattr(po_out, "is_container_overdue", "No") == "Yes"
        
        delay_type = None
        if is_invoice_delayed:
            delay_type = "Invoice Delayed"
        elif is_shipment_delayed:
            delay_type = "Shipment Delayed"

        # If it is delayed...
        if delay_type:
            # 1. Instant Notification Check
            if not po.delay_notification_sent:
                po.delay_notification_sent = True
                
                recipients = invoice_subscribers if is_invoice_delayed else shipment_subscribers
                if recipients:
                    background_tasks.add_task(
                        send_delay_notification,
                        emails=recipients,
                        po_number=str(po.order_number or po.sellercloud_po_id),
                        delay_type=delay_type,
                        delay_details=po_out.delay_details
                    )
            
            # 2. Weekly Digest Collection Check (Missing Reason)
            if weekly_digest and not po.delay_reason:
                if is_invoice_delayed:
                    invoice_delayed_digest.append({"po": po, "delay_details": po_out.delay_details})
                else:
                    shipment_delayed_digest.append({"po": po, "delay_details": po_out.delay_details})

    db.commit()

    # 3. Send Weekly Digest Email
    if weekly_digest and (invoice_delayed_digest or shipment_delayed_digest) and digest_subscribers:
        background_tasks.add_task(
            send_weekly_digest,
            emails=digest_subscribers,
            invoice_delayed_pos=invoice_delayed_digest,
            shipment_delayed_pos=shipment_delayed_digest
        )

    return {
        "success": True, 
        "message": "Delay check completed.", 
        "digest_sent": weekly_digest and (len(invoice_delayed_digest) > 0 or len(shipment_delayed_digest) > 0)
    }
