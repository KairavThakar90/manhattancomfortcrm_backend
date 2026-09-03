from sqlalchemy.orm import Session
from app import models
import uuid

from datetime import datetime, timezone

def log_activity(
    db: Session, 
    action: str, 
    user_id: uuid.UUID = None, 
    category: str = None,
    entity_type: str = None, 
    entity_id: str = None, 
    details: dict = None
):
    """
    Utility function to quickly log user or system activity.
    """
    if not category:
        if action in ["LOGIN", "LOGIN_GOOGLE", "LOGIN_BYPASS_2FA", "REGISTER"]:
            category = "AUTH"
        elif entity_type == "CONTAINER" or "CONTAINER" in action:
            category = "CONTAINER"
        elif entity_type == "PURCHASE_ORDER" or "PO_" in action:
            category = "PURCHASE_ORDER"
        elif "COMMENT" in action:
            category = "COMMUNICATION"
        else:
            category = "SYSTEM"
            
    try:
        log_entry = models.UserActivityLog(
            user_id=user_id,
            action=action,
            category=category,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            details=details,
            created_at=datetime.now(timezone.utc)
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Failed to log activity '{action}': {e}")

def _clean_val_for_display(val, field_name=""):
    if val is None or val == "":
        return "none"
    val_str = str(val).strip()
    # Strip time from date/datetime strings (e.g. 2026-08-22T00:00:00+00:00 or 2026-08-22 00:00:00)
    if "date" in field_name.lower() or ("T" in val_str and len(val_str) >= 10 and val_str[4] == '-' and val_str[7] == '-'):
        if "T" in val_str:
            val_str = val_str.split("T")[0]
        elif " " in val_str and len(val_str) >= 10 and val_str[4] == '-' and val_str[7] == '-':
            val_str = val_str.split(" ")[0]
    return val_str

def generate_human_readable_message(action: str, entity_type: str, entity_id: str, details: dict, user_name: str) -> str:
    name = user_name or "System"
    details = details or {}
    
    if action == "LOGIN":
        return f"{name} logged in."
    elif action == "LOGIN_GOOGLE":
        return f"{name} logged in with Google."
    elif action == "LOGIN_BYPASS_2FA":
        return f"{name} logged in (bypassed 2FA)."
    elif action == "REGISTER":
        return f"{name} registered a new account."
    elif action == "UPDATE_CONTAINER":
        changes = details.get("changes", [])
        if changes:
            change_strs = []
            
            # Handle both list of dicts and dict of dicts
            if isinstance(changes, dict):
                items_to_iterate = [{"field": k, "old": v.get("old"), "new": v.get("new")} for k, v in changes.items()]
            else:
                items_to_iterate = changes
                
            for c in items_to_iterate:
                field_name = c.get("field", "")
                old_val = _clean_val_for_display(c.get("old"), field_name)
                new_val = _clean_val_for_display(c.get("new"), field_name)
                
                # If they normalize to the same value, skip displaying it
                if old_val == new_val:
                    continue

                display_field = field_name.replace("_", " ").title()
                change_strs.append(f"{display_field} changed from {old_val} to {new_val}")
            
            if change_strs:
                changes_str = ", ".join(change_strs)
                c_name = details.get("container_name") or entity_id
                return f"{name} updated container {c_name}: {changes_str}."
        c_name = details.get("container_name") or entity_id
        return f"{name} updated container {c_name}."
    elif action == "ADD_CONTAINER_COMMENT":
        msg = details.get("message", "")
        if msg:
            return f"{name} commented: {msg}"
        return f"{name} added a comment."
    elif action == "ADD_CONTAINER_ATTACHMENTS":
        count = details.get("files_uploaded", "attachments")
        c_name = details.get("container_name") or entity_id
        return f"{name} uploaded {count} attachments to Shipping Container {c_name}."
    elif action == "DELETE_CONTAINER_ATTACHMENT":
        c_name = details.get("container_name") or entity_id
        return f"{name} deleted an attachment from Shipping Container {c_name}."
    elif action == "ADD_PO_COMMENT":
        po_num = details.get("po_number") or entity_id
        return f"{name} added a comment to Purchase Order {po_num}."
    elif action == "UPDATE_PO_COMMENT":
        po_num = details.get("po_number") or entity_id
        return f"{name} updated a comment on Purchase Order {po_num}."
    elif action == "ADD_PO_ITEM_COMMENT":
        po_num = details.get("po_number") or "unknown"
        return f"{name} added a comment to a Purchase Order Item (PO {po_num})."
    elif action == "UPDATE_PO_ITEM_COMMENT":
        po_num = details.get("po_number") or "unknown"
        return f"{name} updated a comment on a Purchase Order Item (PO {po_num})."
    elif action in ["UPDATE_PO_STATUS", "UPDATE_PO_LEAD_TIME"]:
        changes = details.get("changes", [])
        if changes:
            change_strs = []
            for c in changes:
                field_name = c.get("field", "")
                old_val = _clean_val_for_display(c.get("old"), field_name)
                new_val = _clean_val_for_display(c.get("new"), field_name)
                
                if old_val == new_val:
                    continue

                display_field = field_name.replace("_", " ").title()
                change_strs.append(f"{display_field} changed from {old_val} to {new_val}")
            
            if change_strs:
                changes_str = ", ".join(change_strs)
                return f"{name} updated Purchase Order {entity_id}: {changes_str}."
        return f"{name} updated Purchase Order {entity_id}."
    elif action == "SYNC_PO":
        return f"{name} manually synced Purchase Order {entity_id} from SellerCloud."
    elif action == "UPDATE_PO_ITEM_QUANTITY":
        old_qty = details.get("old_qty", "unknown")
        new_qty = details.get("new_qty", "unknown")
        return f"{name} changed Product Quantity for Item {entity_id} from {old_qty} to {new_qty}."
    elif action == "ADD_ITEM_TO_CONTAINER":
        sku = details.get("sku", "unknown item")
        qty = details.get("qty_added", 0)
        c_name = details.get("container_name") or entity_id
        return f"{name} added {qty} units of {sku} to Container {c_name}."
    elif action == "CONTAINER_ITEM_UPDATE":
        msg = details.get("message")
        c_name = details.get("container_name") or entity_id
        if msg:
            return msg + f" (by {name})"
        return f"{name} updated an item in container {c_name}."
    else:
        # Fallback
        if entity_type and entity_id:
            return f"{name} performed {action} on {entity_type} {entity_id}."
        return f"{name} performed {action}."
