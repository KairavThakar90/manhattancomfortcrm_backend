from fastapi import APIRouter, Depends, Query, BackgroundTasks, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app import models
from app.schemas import PurchaseOrderOut
from app.services.email_service import send_aggregated_delay_notification
from app.config import settings
import os

router = APIRouter(prefix="/cron", tags=["Cron Jobs"])

CRON_SECRET = os.getenv("CRON_SECRET", "my-secret-cron-key")

@router.post("/test-delays")
async def test_delays(
    background_tasks: BackgroundTasks,
    weekly_digest: bool = Query(True, description="Send the weekly digest email for missing delay reasons"),
    db: Session = Depends(get_db)
):
    """
    Test endpoint for delay notifications. 
    Sends the digest exclusively to projectmanager663@gmail.com.
    """
    active_pos = db.query(models.PurchaseOrder).filter(
        models.PurchaseOrder.status.notin_(["SHIPPED", "COMPLETED", "CANCELED"])
    ).all()

    invoice_delayed_digest = []
    shipment_delayed_digest = []

    for po in active_pos:
        po_out = PurchaseOrderOut.model_validate(po)
        is_invoice_delayed = getattr(po_out, "is_invoice_delayed", "No") == "Yes"
        is_shipment_delayed = getattr(po_out, "is_container_overdue", "No") == "Yes"
        
        # Collect digest info regardless of reason being filled or not for testing purposes
        if is_invoice_delayed:
            invoice_delayed_digest.append({"po": po, "delay_details": getattr(po_out, "delay_details", "")})
        elif is_shipment_delayed:
            shipment_delayed_digest.append({"po": po, "delay_details": getattr(po_out, "delay_details", "")})

    target_email = "projectmanager663@gmail.com"
    
    if invoice_delayed_digest or shipment_delayed_digest:
        background_tasks.add_task(
            send_aggregated_delay_notification,
            emails=[target_email],
            invoice_delayed_pos=invoice_delayed_digest,
            shipment_delayed_pos=shipment_delayed_digest,
            is_weekly_digest=weekly_digest
        )
        return {"success": True, "message": f"Test email dispatched to {target_email}", "invoice_delays": len(invoice_delayed_digest), "shipment_delays": len(shipment_delayed_digest)}
    
    return {"success": True, "message": "No delayed POs found to send."}

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
    
    instant_invoice_alerts = []
    instant_shipment_alerts = []
    
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
                if is_invoice_delayed:
                    instant_invoice_alerts.append({"po": po, "delay_details": po_out.delay_details})
                else:
                    instant_shipment_alerts.append({"po": po, "delay_details": po_out.delay_details})
            
            # 2. Weekly Digest Collection Check (Missing Reason)
            if weekly_digest and not po.delay_reason:
                if is_invoice_delayed:
                    invoice_delayed_digest.append({"po": po, "delay_details": po_out.delay_details})
                else:
                    shipment_delayed_digest.append({"po": po, "delay_details": po_out.delay_details})

    db.commit()

    # 3. Send Aggregated Instant Email
    if (instant_invoice_alerts or instant_shipment_alerts) and digest_subscribers:
        background_tasks.add_task(
            send_aggregated_delay_notification,
            emails=digest_subscribers,
            invoice_delayed_pos=instant_invoice_alerts,
            shipment_delayed_pos=instant_shipment_alerts,
            is_weekly_digest=False
        )

    # 4. Send Weekly Digest Email
    if weekly_digest and (invoice_delayed_digest or shipment_delayed_digest) and digest_subscribers:
        background_tasks.add_task(
            send_aggregated_delay_notification,
            emails=digest_subscribers,
            invoice_delayed_pos=invoice_delayed_digest,
            shipment_delayed_pos=shipment_delayed_digest,
            is_weekly_digest=True
        )

    return {
        "success": True, 
        "message": "Delay check completed.", 
        "digest_sent": weekly_digest and (len(invoice_delayed_digest) > 0 or len(shipment_delayed_digest) > 0)
    }
