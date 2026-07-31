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

async def send_tag_notification(emails: list[str], commenter_name: str, link: str, is_edit: bool = False):
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping email notification.")
        return

    subject = f"Manhattan CRM: {commenter_name} mentioned you in a comment"
    if is_edit:
        subject = f"Manhattan CRM: {commenter_name} edited a comment you were mentioned in"

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px;">
        <h2>You were mentioned!</h2>
        <p><strong>{commenter_name}</strong> has {'edited a comment mentioning you' if is_edit else 'mentioned you in a comment'}.</p>
        <p>Click the link below to view it:</p>
        <a href="{link}" style="display: inline-block; padding: 10px 15px; background-color: #007bff; color: white; text-decoration: none; border-radius: 5px;">View Comment</a>
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
        logger.info(f"Sent notification email to {emails}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
