from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from app.config import settings
import logging

logger = logging.getLogger(__name__)

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
    comment_text: str = ""
):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping email notification.")
        return

    action_text = "edited a comment you were mentioned in" if is_edit else "mentioned you in a comment"
    
    subject = f"Manhattan Comfort Dashboard - {section}"
    if po_number:
        subject += f" (PO #{po_number}"
        if sku:
            subject += f", SKU: {sku}"
        subject += ")"

    details_html = ""
    if po_number:
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

    message = MessageSchema(**message_kwargs)

    fm = FastMail(conf)
    try:
        await fm.send_message(message)
        logger.info(f"Sent notification email to TO: {to_email}, CC: {cc_emails}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

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
