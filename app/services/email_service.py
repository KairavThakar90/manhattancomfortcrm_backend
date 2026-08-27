from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from app.config import settings
import logging

logger = logging.getLogger(__name__)

EMAIL_SIGNATURE_HTML = """
<br><br>
<table cellpadding="0" cellspacing="0" border="0" style="margin-top: 30px; border-collapse: collapse; font-family: Arial, sans-serif; width: 100%; max-width: 600px;">
    <tr>
        <td style="background-color: #f4efeb; padding: 20px; text-align: center; vertical-align: middle; width: 40%;">
            <a href="https://www.manhattancomfort.com">
                <img src="https://storage.googleapis.com/manhattancomfort-crm-backend-storage/email-assets/manhattan-comfort-logo-unnamed.png" alt="Manhattan Comfort" style="max-width: 150px; margin-bottom: 15px; border: none;" />
            </a>
            <br />
            <!-- Social Icons -->
            <a href="https://www.facebook.com/ManhattanComfort" style="text-decoration: none; margin: 0 4px;"><img src="https://img.icons8.com/ios-filled/50/000000/facebook-circled--v1.png" width="22" alt="Facebook" style="border: none;" /></a>
            <a href="https://www.instagram.com/manhattancomfort" style="text-decoration: none; margin: 0 4px;"><img src="https://img.icons8.com/ios-filled/50/000000/instagram-new--v1.png" width="22" alt="Instagram" style="border: none;" /></a>
            <a href="https://x.com/ManhattanComfor" style="text-decoration: none; margin: 0 4px;"><img src="https://img.icons8.com/ios-filled/50/000000/twitterx--v2.png" width="22" alt="X" style="border: none;" /></a>
            <a href="https://www.pinterest.com/manhattancomfor/" style="text-decoration: none; margin: 0 4px;"><img src="https://img.icons8.com/ios-filled/50/000000/pinterest--v1.png" width="22" alt="Pinterest" style="border: none;" /></a>
            <a href="https://www.linkedin.com/company/manhattan-comfort/" style="text-decoration: none; margin: 0 4px;"><img src="https://img.icons8.com/ios-filled/50/000000/linkedin-circled--v1.png" width="22" alt="LinkedIn" style="border: none;" /></a>
        </td>
        <td style="background-color: #000000; color: #ffffff; padding: 25px 30px; vertical-align: middle; width: 60%;">
            <h3 style="margin: 0 0 5px 0; font-size: 18px; color: #ffffff; font-weight: bold;">Manhattan Comfort</h3>
            <h3 style="margin: 0 0 20px 0; font-size: 18px; color: #ffffff; font-weight: bold;">Team</h3>
            
            <table cellpadding="0" cellspacing="0" border="0" style="color: #ffffff; font-size: 12px; line-height: 1.5;">
                <tr>
                    <td style="padding-bottom: 8px; padding-right: 10px; font-size: 14px; text-align: center;">@</td>
                    <td style="padding-bottom: 8px;"><a href="mailto:help@manhattancomfort.com" style="color: #ffffff; text-decoration: none;">help@manhattancomfort.com</a></td>
                </tr>
                <tr>
                    <td style="padding-bottom: 8px; padding-right: 10px; font-size: 14px; text-align: center;">📱</td>
                    <td style="padding-bottom: 8px;"><a href="tel:9088880818" style="color: #ffffff; text-decoration: none;">(908) 888-0818</a></td>
                </tr>
                <tr>
                    <td style="padding-right: 10px; font-size: 14px; text-align: center;">🌐</td>
                    <td><a href="https://www.manhattancomfort.com" style="color: #ffffff; text-decoration: none;">www.manhattancomfort.com</a></td>
                </tr>
            </table>
        </td>
    </tr>
</table>
"""

conf = ConnectionConfig(
    MAIL_USERNAME=settings.SMTP_USER,
    MAIL_PASSWORD=settings.SMTP_PASS,
    MAIL_FROM=settings.SMTP_FROM or "noreply@manhattancrm.com",
    MAIL_PORT=settings.SMTP_PORT,
    MAIL_SERVER=settings.SMTP_HOST,
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True
)

async def send_2fa_email(email_to: str, code: str, first_name: str = "User"):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping 2FA email.")
        return

    subject = "Your Manhattan CRM 2FA Login Code"
    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #333;">Hello {first_name},</h2>
        <p>Your 2-Factor Authentication code is:</p>
        <div style="font-size: 32px; font-weight: bold; letter-spacing: 5px; color: #007bff; margin: 20px 0; text-align: center;">
            {code}
        </div>
        <p>This code will expire in 10 minutes. If you did not request this, please ignore this email.</p>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """
    
    message = MessageSchema(
        subject=subject,
        recipients=[email_to],
        body=html,
        subtype=MessageType.html
    )
    
    try:
        fm = FastMail(conf)
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send 2FA email to {email_to}: {str(e)}")

async def send_tag_notification(
    emails: list[str], 
    commenter_name: str, 
    link: str, 
    is_edit: bool = False, 
    section: str = "Purchase Orders", 
    po_number: str = None, 
    sku: str = None, 
    comment_text: str = "",
    attachments: list = None,
    container_name: str = None
):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping email notification.")
        return

    action_text = "edited a comment you were mentioned in" if is_edit else "mentioned you in a comment"
    
    subject = f"Manhattan Comfort Dashboard - {section}"
    if container_name:
        subject += f" (Container: {container_name})"
    elif po_number:
        subject += f" (PO #{po_number}"
        if sku:
            subject += f", SKU: {sku}"
        subject += ")"

    details_html = ""
    if container_name:
        details_html += f"<p style='margin: 0 0 5px 0;'><strong>Container Name:</strong> {container_name}</p>"
    elif po_number:
        details_html += f"<p style='margin: 0 0 5px 0;'><strong>PO Number:</strong> {po_number}</p>"
    if sku:
        details_html += f"<p style='margin: 0 0 5px 0;'><strong>SKU:</strong> {sku}</p>"

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #333;">You were mentioned!</h2>
        <p><strong>{commenter_name}</strong> has {action_text}.</p>
        
        <div style="background-color: #f9f9f9; padding: 15px; border-left: 4px solid #007bff; margin: 20px 0;">
            {details_html}
            <p style='margin: 15px 0 5px 0;'><strong>Comment:</strong></p>
            <p style="white-space: pre-wrap; margin: 0;">{comment_text}</p>
        </div>
        
        <p>Click the button below to view it in the dashboard:</p>
        <a href="{link}" style="display: inline-block; padding: 10px 20px; background-color: #007bff; color: white; text-decoration: none; border-radius: 5px; margin-top: 10px;">View Comment</a>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """

    if not emails:
        return

    # Add all tagged users as CC recipients (put first in TO, rest in CC)
    to_email = emails[0]
    cc_emails = emails[1:] if len(emails) > 1 else None

    message_kwargs = {
        "subject": subject,
        "recipients": [to_email],
        "body": html,
        "subtype": MessageType.html
    }
    if cc_emails:
        message_kwargs["cc"] = cc_emails
        
    if attachments:
        from fastapi import UploadFile
        from starlette.datastructures import Headers
        import io
        
        formatted_attachments = []
        for att in attachments:
            file_content = io.BytesIO(att["content"])
            headers = Headers({"content-type": att.get("content_type") or "application/octet-stream"})
            u = UploadFile(filename=att["file_name"], file=file_content, headers=headers)
            formatted_attachments.append(u)
            
        message_kwargs["attachments"] = formatted_attachments

    message = MessageSchema(**message_kwargs)

    fm = FastMail(conf)
    try:
        await fm.send_message(message)
        logger.info(f"Sent notification email to TO: {to_email}, CC: {cc_emails}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        import traceback
        with open("email_error.txt", "a") as f:
            f.write(traceback.format_exc() + "\n")

async def send_welcome_email(email_to: str, password: str, login_link: str, first_name: str = ""):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping welcome email.")
        return

    subject = "Welcome to Manhattan CRM - Your Account Details"
    
    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px;">
        <h2>Welcome to Manhattan CRM, {first_name}!</h2>
        <p>An account has been created for you.</p>
        <p><strong>Your login details:</strong></p>
        <ul>
            <li><strong>Email:</strong> {email_to}</li>
            <li><strong>Password:</strong> {password}</li>
        </ul>
        <p>Please click the link below to log in, and we recommend changing your password after your first login.</p>
        <br>
        <a href="{login_link}" style="display: inline-block; padding: 10px 15px; background-color: #007bff; color: white; text-decoration: none; border-radius: 5px;">Log In to CRM</a>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """

    message = MessageSchema(
        subject="Welcome to Manhattan CRM",
        recipients=[email_to],
        body=html,
        subtype=MessageType.html
    )

    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send welcome email to {email_to}: {e}")

async def send_po_status_update_email(
    db, 
    po_number: str, 
    old_status: str, 
    new_status: str, 
    vendor_name: str
):
    from app.models import User
    
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping email notification.")
        return

    # Find all admins
    admins = db.query(User).filter(User.role == "admin").all()
    emails = [admin.email for admin in admins if admin.email]
    
    if not emails:
        return

    subject = f"PO #{po_number} Status Update - {vendor_name}"
    
    changes_html = ""
    if old_status != new_status:
        changes_html += f"<p><strong>Status:</strong> {old_status or 'None'} &rarr; {new_status or 'None'}</p>"

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #333;">Purchase Order Status Updated</h2>
        <p style="font-size: 16px; color: #555;">
            Vendor <strong>{vendor_name}</strong> has updated the status for PO <strong>#{po_number}</strong>.
        </p>
        <div style="background-color: #f9f9f9; padding: 15px; border-radius: 4px; margin-top: 15px; border-left: 4px solid #007bff;">
            {changes_html}
        </div>
        <div style="margin-top: 30px; padding-top: 15px; border-top: 1px solid #eee; font-size: 12px; color: #999;">
            <p>This is an automated notification from the Manhattan Comfort Dashboard.</p>
        </div>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """

    message = MessageSchema(
        subject=subject,
        recipients=emails,
        body=html,
        subtype=MessageType.html
    )

    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send PO status update email: {e}")


async def send_container_emptied_notification(email_to: str, container_name: str, date_emptied: str, door_name: str = None, date_dropped_off: str = None, cc_emails: list = None):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        return

    subject = f"Container Emptied: {container_name}"
    # Try to parse and reformat date
    display_date = date_emptied
    try:
        from datetime import datetime
        dt = datetime.strptime(date_emptied, "%Y-%m-%d")
        display_date = dt.strftime("%B %d, %Y")
    except Exception:
        pass
        
    display_dropped_date = date_dropped_off if date_dropped_off else "N/A"
    try:
        if date_dropped_off:
            dt_dropped = datetime.strptime(date_dropped_off, "%Y-%m-%d")
            display_dropped_date = dt_dropped.strftime("%B %d, %Y")
    except Exception:
        pass
        
    door_val = door_name if door_name else "N/A"
    
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; color: #333;">
        <p>Hello,</p>
        <p>This is to notify you that the following container has been successfully emptied:</p>
        
        <table style="width: 100%; border-collapse: collapse; margin: 20px 0; border: 1px solid #ddd;">
            <tr style="background-color: #f2f2f2;">
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold; width: 40%;">Details</td>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold; width: 60%;">Information</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">Container Number</td>
                <td style="padding: 12px; border: 1px solid #ddd;">{container_name}</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">Status</td>
                <td style="padding: 12px; border: 1px solid #ddd;">Emptied</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">Dropped Date</td>
                <td style="padding: 12px; border: 1px solid #ddd;">{display_dropped_date}</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">Empty Date</td>
                <td style="padding: 12px; border: 1px solid #ddd;">{display_date}</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">Door</td>
                <td style="padding: 12px; border: 1px solid #ddd;">{door_val}</td>
            </tr>
        </table>
        
        <p>Best regards,</p>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """
    
    message_kwargs = {
        "subject": subject,
        "recipients": [email_to],
        "body": html,
        "subtype": MessageType.html
    }
    if cc_emails:
        message_kwargs["cc"] = cc_emails

    message = MessageSchema(**message_kwargs)
    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send container emptied notification: {e}")


async def send_admin_new_user_notification(admin_emails: list, new_user_name: str, new_user_email: str, new_user_role: str):
    if not settings.SMTP_USER or not settings.SMTP_PASS or not admin_emails:
        return

    subject = f"New User Registration: {new_user_name}"
    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #333;">New User Registration</h2>
        <p>A new user has just registered on the platform:</p>
        <ul>
            <li><strong>Name:</strong> {new_user_name}</li>
            <li><strong>Email:</strong> {new_user_email}</li>
            <li><strong>Role:</strong> {new_user_role}</li>
        </ul>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """
    
    message = MessageSchema(subject=subject, recipients=admin_emails, body=html, subtype=MessageType.html)
    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send new user admin notification: {e}")


async def send_delay_notification(emails: list, po_number: str, delay_type: str, delay_details: dict = None):
    if not settings.SMTP_USER or not settings.SMTP_PASS or not emails:
        return

    subject = f"ACTION REQUIRED: PO #{po_number} is {delay_type}"
    
    details_html = ""
    if delay_details and delay_type == "Shipment Delayed":
        details_html += "<ul style='margin-bottom: 20px;'>"
        if delay_details.get("arrived_containers"):
            details_html += "<li><strong>Arrived Containers:</strong> " + ", ".join(delay_details["arrived_containers"]) + "</li>"
        if delay_details.get("delayed_containers"):
            details_html += "<li><strong style='color: #dc3545;'>Delayed Containers:</strong> " + ", ".join(delay_details["delayed_containers"]) + "</li>"
        if delay_details.get("unassigned_delayed_items"):
            details_html += "<li><strong style='color: #dc3545;'>Unassigned Items:</strong> Yes (Not yet in a container)</li>"
        details_html += "</ul>"

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px; border-left: 5px solid #dc3545;">
        <h2 style="color: #dc3545;">Purchase Order Delayed</h2>
        <p>Purchase Order <strong>#{po_number}</strong> has just been flagged as <strong>{delay_type}</strong>.</p>
        {details_html}
        <p>Please log in to the system and provide a Delay Reason as soon as possible.</p>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """
    
    message = MessageSchema(subject=subject, recipients=emails, body=html, subtype=MessageType.html)
    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send delay notification: {e}")


async def send_aggregated_delay_notification(emails: list, invoice_delayed_pos: list, shipment_delayed_pos: list, is_weekly_digest: bool = False):
    if not settings.SMTP_USER or not settings.SMTP_PASS or not emails:
        return

    if is_weekly_digest:
        subject = "Weekly Digest: Delayed Purchase Orders Requiring Action"
        header_text = "Weekly Delayed PO Digest"
        description_text = "The following Purchase Orders are currently delayed and <strong>do not have a delay reason provided:</strong>"
    else:
        subject = "ACTION REQUIRED: New Delayed Purchase Orders"
        header_text = "New Delayed Purchase Orders"
        description_text = "The following Purchase Orders have just been flagged as delayed. Please log in and provide a Delay Reason as soon as possible:"
    
    invoice_list_html = ""
    for item in invoice_delayed_pos:
        po = item.get("po") if isinstance(item, dict) else item
        po_num = getattr(po, "order_number", None) or po.sellercloud_po_id
        invoice_list_html += f"<tr><td style='padding: 8px; border: 1px solid #ddd;'>PO #{po_num}</td></tr>"
    if not invoice_list_html:
        invoice_list_html = "<tr><td style='padding: 8px; border: 1px solid #ddd;'>None</td></tr>"

    shipment_list_html = ""
    for item in shipment_delayed_pos:
        po = item.get("po") if isinstance(item, dict) else item
        details = item.get("delay_details", {}) if isinstance(item, dict) else {}
        po_num = getattr(po, "order_number", None) or po.sellercloud_po_id
        
        arrived = ", ".join(details.get("arrived_containers", [])) if details.get("arrived_containers") else "None"
        delayed = ", ".join(details.get("delayed_containers", [])) if details.get("delayed_containers") else "None"
        unassigned = "Yes" if details.get("unassigned_delayed_items") else "No"
        
        shipment_list_html += f"<tr><td style='padding: 8px; border: 1px solid #ddd;'>PO #{po_num}</td>"
        shipment_list_html += f"<td style='padding: 8px; border: 1px solid #ddd;'>{arrived}</td>"
        shipment_list_html += f"<td style='padding: 8px; border: 1px solid #ddd; color: #c0392b;'>{delayed}</td>"
        shipment_list_html += f"<td style='padding: 8px; border: 1px solid #ddd; color: #c0392b;'>{unassigned}</td></tr>"

    if not shipment_list_html:
        shipment_list_html = "<tr><td colspan='4' style='padding: 8px; border: 1px solid #ddd; text-align: center;'>None</td></tr>"

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px;">
        <h2 style="color: #333;">{header_text}</h2>
        <p>{description_text}</p>
        
        <h3 style="color: #d35400;">Invoice Delayed</h3>
        <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
            <thead>
                <tr style="background-color: #f2f2f2;">
                    <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">PO Number</th>
                </tr>
            </thead>
            <tbody>{invoice_list_html}</tbody>
        </table>
        
        <h3 style="color: #c0392b;">Shipment Delayed</h3>
        <table style="width: 100%; border-collapse: collapse; margin-top: 10px;">
            <thead>
                <tr style="background-color: #f2f2f2;">
                    <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">PO Number</th>
                    <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Arrived Containers</th>
                    <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Delayed Containers</th>
                    <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Unassigned Items</th>
                </tr>
            </thead>
            <tbody>{shipment_list_html}</tbody>
        </table>
        
        <p style="margin-top: 20px;">Please log in to the dashboard to update these POs.</p>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """
    
    message = MessageSchema(subject=subject, recipients=emails, body=html, subtype=MessageType.html)
    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send weekly digest: {e}")


async def send_container_lifecycle_tag_notification(
    emails: list[str],
    commenter_name: str,
    link: str,
    field_name: str,  # "Vendor Credit Needed" or "Receiving Closure Notes"
    field_value: str,
    container_name: str,
    *args,
    **kwargs
):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping email notification.")
        return

    subject = f"Manhattan Comfort Dashboard - Container {container_name} ({field_name})"

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; border: 1px solid #eee; border-radius: 5px; color: #333;">
        <h2 style="color: #007bff; margin-top: 0;">You were mentioned in Container {container_name}!</h2>
        <p><strong>{commenter_name}</strong> has tagged you in the <strong>{field_name}</strong> section.</p>
        
        <table style="width: 100%; border-collapse: collapse; margin-top: 20px; border: 1px solid #ddd;">
            <tr style="background-color: #f2f2f2;">
                <th style="padding: 12px; border: 1px solid #ddd; text-align: left; width: 30%;">Field</th>
                <th style="padding: 12px; border: 1px solid #ddd; text-align: left; width: 70%;">Details</th>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">Container Name</td>
                <td style="padding: 12px; border: 1px solid #ddd;">{container_name}</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #ddd; font-weight: bold;">{field_name}</td>
                <td style="padding: 12px; border: 1px solid #ddd; white-space: pre-wrap;">{field_value}</td>
            </tr>
        </table>
        {EMAIL_SIGNATURE_HTML}
    </div>
    """

    if not emails:
        return

    to_email = emails[0]
    cc_list = emails[1:] if len(emails) > 1 else []

    message = MessageSchema(
        subject=subject,
        recipients=[to_email],
        cc=cc_list,
        body=html,
        subtype=MessageType.html
    )
    
    try:
        fm = FastMail(conf)
        await fm.send_message(message)
    except Exception as e:
        logger.error(f"Failed to send container lifecycle tag notification: {str(e)}")

