from sqlalchemy.orm import Session
from app import models
import uuid

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
            details=details
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Failed to log activity '{action}': {e}")

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
            for c in changes:
                field_name = c["field"].replace("_", " ").title()
                
                old_val = c.get("old")
                if old_val is None or old_val == "": old_val = "none"
                
                new_val = c.get("new")
                if new_val is None or new_val == "": new_val = "none"
                
                change_strs.append(f"{field_name} changed from {old_val} to {new_val}")
            
            changes_str = ", ".join(change_strs)
            return f"{name} updated the container: {changes_str}."
        return f"{name} updated the container."
    elif action == "ADD_CONTAINER_COMMENT":
        msg = details.get("message", "")
        if msg:
            return f"{name} commented: {msg}"
        return f"{name} added a comment."
    elif action == "ADD_CONTAINER_ATTACHMENTS":
        count = details.get("files_uploaded", "attachments")
        return f"{name} uploaded {count} attachments to Shipping Container {entity_id}."
    elif action == "DELETE_CONTAINER_ATTACHMENT":
        return f"{name} deleted an attachment from Shipping Container {entity_id}."
    elif action == "ADD_PO_COMMENT":
        return f"{name} added a comment to Purchase Order {entity_id}."
    elif action == "UPDATE_PO_COMMENT":
        return f"{name} updated a comment on Purchase Order {entity_id}."
    elif action == "ADD_PO_ITEM_COMMENT":
        return f"{name} added a comment to a Purchase Order Item (PO {entity_id})."
    elif action == "UPDATE_PO_ITEM_COMMENT":
        return f"{name} updated a comment on a Purchase Order Item (PO {entity_id})."
    elif action in ["UPDATE_PO_STATUS", "UPDATE_PO_LEAD_TIME"]:
        changes = details.get("changes", [])
        if changes:
            change_strs = []
            for c in changes:
                field_name = c["field"].replace("_", " ").title()
                
                old_val = c.get("old")
                if old_val is None or old_val == "": old_val = "none"
                
                new_val = c.get("new")
                if new_val is None or new_val == "": new_val = "none"
                
                change_strs.append(f"{field_name} changed from {old_val} to {new_val}")
            
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
        return f"{name} added {qty} units of {sku} to Container {entity_id}."
    elif action == "CONTAINER_ITEM_UPDATE":
        msg = details.get("message")
        if msg:
            return msg + f" (by {name})"
        return f"{name} updated an item in container {entity_id}."
    else:
        # Fallback
        if entity_type and entity_id:
            return f"{name} performed {action} on {entity_type} {entity_id}."
        return f"{name} performed {action}."
