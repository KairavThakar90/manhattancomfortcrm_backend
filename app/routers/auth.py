from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Query
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from datetime import datetime, timedelta
import random
import httpx
from typing import Union, Optional, Any, List, Dict
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from app.database import get_db
from app import auth as auth_utils
from app import models
import uuid
from app.services.email_service import send_welcome_email, send_2fa_email, send_admin_new_user_notification
from app.services.activity_service import log_activity
from app.config import settings
from app.schemas import (
    Token, RefreshTokenRequest, LogoutResponse, UserOut, UserCreate, UserUpdate, UserMentionOut,
    UpdatePasswordRequest, Login2FAResponse, Verify2FARequest, GoogleLoginRequest, ColumnPreferencesOut,
    VendorSummary
)

router = APIRouter(prefix="/auth", tags=["Auth"])


def enrich_user_out(user: models.User, db: Session) -> UserOut:
    """Enriches UserOut with full details for all assigned vendors."""
    user_out = UserOut.model_validate(user)
    effective_ids = user.effective_vendor_ids
    if effective_ids:
        vendors = db.query(models.Vendor).filter(models.Vendor.id.in_(effective_ids)).all()
        user_out.vendors = [VendorSummary.model_validate(v) for v in vendors]
        user_out.vendor_ids = [v.id for v in vendors]
    else:
        user_out.vendors = []
        user_out.vendor_ids = []
    return user_out


def enrich_users_out(users: list[models.User], db: Session) -> list[UserOut]:
    """Batch enriches a list of users with vendor details without N+1 queries."""
    all_vendor_ids = set()
    for u in users:
        all_vendor_ids.update(u.effective_vendor_ids)
    
    vendor_map = {}
    if all_vendor_ids:
        vendors = db.query(models.Vendor).filter(models.Vendor.id.in_(list(all_vendor_ids))).all()
        vendor_map = {v.id: VendorSummary.model_validate(v) for v in vendors}
        
    results = []
    for u in users:
        u_out = UserOut.model_validate(u)
        u_vids = u.effective_vendor_ids
        u_out.vendors = [vendor_map[vid] for vid in u_vids if vid in vendor_map]
        u_out.vendor_ids = [vid for vid in u_vids if vid in vendor_map]
        results.append(u_out)
    return results


def resolve_vendor_helper(db: Session, raw_id: Any) -> Optional[models.Vendor]:
    """Helper to resolve a Vendor by UUID, SellerCloud Integer ID, or Name."""
    if not raw_id:
        return None
    raw_str = str(raw_id).strip()
    try:
        v_uuid = uuid.UUID(raw_str)
        vendor = db.query(models.Vendor).filter(models.Vendor.id == v_uuid).first()
        if vendor:
            return vendor
    except ValueError:
        pass
    if raw_str.isdigit():
        vendor = db.query(models.Vendor).filter(models.Vendor.sellercloud_vendor_id == int(raw_str)).first()
        if vendor:
            return vendor
    return db.query(models.Vendor).filter(models.Vendor.name.ilike(raw_str)).first()


@router.post("/login", response_model=Union[Login2FAResponse, Token])
def login(background_tasks: BackgroundTasks, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    Step 1 of Login: Verify credentials and send 2FA email.
    """
    email = form_data.username.lower() if form_data.username else form_data.username
    user = auth_utils.authenticate_user(db, email, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    # Generate 6 digit OTP
    otp = str(random.randint(100000, 999999))
    user.otp_code = otp
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    db.commit()

    # Bypass 2FA for specific service account
    if email == "googlecloudcron@manhattancomfort.com":
        access_token = auth_utils.create_access_token(data={"sub": str(user.id)})
        refresh_token = auth_utils.create_refresh_token(data={"sub": str(user.id)})
        user.last_login = datetime.utcnow()
        db.commit()
        log_activity(db, action="LOGIN_BYPASS_2FA", user_id=user.id, entity_type="USER", entity_id=str(user.id))
        return Token(
            access_token=access_token,
            refresh_token=refresh_token,
            user=UserOut.model_validate(user)
        )

    # Send email in background
    greeting_name = user.full_name or user.first_name or user.email
    background_tasks.add_task(send_2fa_email, email_to=user.email, code=otp, first_name=greeting_name)

    return Login2FAResponse(
        message="2FA code sent to your email",
        requires_2fa=True,
        email=user.email
    )


@router.post("/google", response_model=Token)
def google_login(request: GoogleLoginRequest, db: Session = Depends(get_db)):
    """
    Login via Google: Verify Google JWT and return access tokens.
    """
    try:
        # Support both Access Tokens (ya29...) and ID Tokens (JWT)
        if request.token.startswith("ya29."):
            response = httpx.get(f"https://oauth2.googleapis.com/tokeninfo?access_token={request.token}")
            if response.status_code != 200:
                raise ValueError("Invalid access token")
            idinfo = response.json()
        else:
            idinfo = id_token.verify_oauth2_token(
                request.token, 
                google_requests.Request(), 
                settings.GOOGLE_CLIENT_ID
            )
            
        email = idinfo.get("email")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google token did not contain an email",
            )
        email = email.lower()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token",
        )

    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not registered",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
        )

    access_token = auth_utils.create_access_token(data={"sub": str(user.id)})
    refresh_token = auth_utils.create_refresh_token(data={"sub": str(user.id)})
    user.last_login = datetime.utcnow()
    db.commit()
    log_activity(db, action="LOGIN_GOOGLE", user_id=user.id, entity_type="USER", entity_id=str(user.id))
    
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserOut.model_validate(user)
    )


@router.post("/verify-2fa", response_model=Token)
def verify_2fa(request: Verify2FARequest, db: Session = Depends(get_db)):
    """
    Step 2 of Login: Verify the OTP code from email.
    """
    email = request.email.lower() if request.email else request.email
    user = db.query(models.User).filter(models.User.email == email).first()
    
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
    if not user.otp_code or user.otp_code != request.code:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA code")
        
    if user.otp_expires_at and datetime.utcnow() > user.otp_expires_at.replace(tzinfo=None):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="2FA code expired")

    # Clear OTP and update last_login
    user.otp_code = None
    user.otp_expires_at = None
    user.last_login = datetime.utcnow()
    db.commit()

    access_token = auth_utils.create_access_token(data={"sub": str(user.id)})
    refresh_token = auth_utils.create_refresh_token(data={"sub": str(user.id)})
    
    # Log the activity
    log_activity(db, action="LOGIN", user_id=user.id, entity_type="USER", entity_id=str(user.id))
    
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserOut.model_validate(user)
    )


@router.post("/refresh", response_model=Token)
def refresh_token(request: RefreshTokenRequest, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh_token for a new access_token and refresh_token.
    The old refresh token is blacklisted to prevent reuse.
    """
    # Check if refresh token is blacklisted
    blacklisted = db.query(models.TokenBlacklist).filter(
        models.TokenBlacklist.token == request.refresh_token
    ).first()
    if blacklisted:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )
    
    # Decode and validate refresh token
    try:
        payload = auth_utils.decode_token(request.refresh_token)
        user_id = payload.get("sub")
        token_type = payload.get("type")
        
        if not user_id or token_type != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )
    except HTTPException:
        raise
    
    # Get user
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    
    # Blacklist the old refresh token to prevent reuse
    exp_timestamp = payload.get("exp")
    expires_at = datetime.utcfromtimestamp(exp_timestamp) if exp_timestamp else datetime.utcnow()
    
    db.add(models.TokenBlacklist(
        token=request.refresh_token,
        expires_at=expires_at
    ))
    db.commit()
    
    # Create new tokens
    new_access_token = auth_utils.create_access_token(data={"sub": str(user.id)})
    new_refresh_token = auth_utils.create_refresh_token(data={"sub": str(user.id)})
    
    return Token(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        user=UserOut.model_validate(user)
    )


@router.post("/logout", response_model=LogoutResponse)
def logout(
    current_user: models.User = Depends(auth_utils.get_current_user),
    token: str = Depends(auth_utils.oauth2_scheme),
    db: Session = Depends(get_db)
):
    """
    Logout the current user by blacklisting their access token.
    The token will no longer be valid for authentication.
    
    Note: If you also have a refresh_token, you should blacklist it separately
    or just delete it on the client side.
    """
    # Decode token to get expiration
    payload = auth_utils.decode_token(token)
    exp_timestamp = payload.get("exp")
    expires_at = datetime.utcfromtimestamp(exp_timestamp) if exp_timestamp else datetime.utcnow()
    
    # Add token to blacklist
    db.add(models.TokenBlacklist(
        token=token,
        expires_at=expires_at
    ))
    db.commit()
    
    return LogoutResponse(message="Successfully logged out")


@router.get("/me", response_model=UserOut)
def read_current_user(current_user=Depends(auth_utils.get_current_user), db: Session = Depends(get_db)):
    """Get current authenticated user information."""
    return enrich_user_out(current_user, db)


@router.get("/me/column-preferences", response_model=ColumnPreferencesOut)
def get_user_column_preferences(current_user=Depends(auth_utils.get_current_user)):
    """Get the current user's column preferences for PO and Container listings."""
    return {
        "po_columns": current_user.po_columns or {},
        "container_columns": current_user.container_columns or {}
    }


@router.get("/users", response_model=list[UserOut])
def get_all_users(
    search: Optional[str] = None,
    role: Optional[str] = None,
    current_user: models.User = Depends(auth_utils.get_current_user),
    db: Session = Depends(get_db)
):
    """Get a list of all active users, for tagging/mentioning in comments."""
    query = db.query(models.User).options(
        joinedload(models.User.vendor),
        joinedload(models.User.warehouse)
    ).filter(
        models.User.is_active == True,
        models.User.email != "googlecloudcron@manhattancomfort.com"
    )
    
    if role:
        query = query.filter(models.User.role == role)
        
    if search:
        search_term = f"{search}%"
        query = query.filter(
            or_(
                models.User.first_name.ilike(search_term),
                models.User.last_name.ilike(search_term),
                models.User.email.ilike(search_term),
                models.User.role.ilike(search_term)
            )
        )
        
    query = query.order_by(models.User.created_at.desc())
    users = query.all()
    return enrich_users_out(users, db)


@router.get("/users/tag", response_model=list[UserMentionOut])
def get_mentionable_users(
    role: Optional[str] = Query(None, description="Optional role filter (e.g. 'vendor', 'warehouse'). If provided, returns all active users of this role, bypassing standard visibility restrictions."),
    current_user: models.User = Depends(auth_utils.get_current_user),
    db: Session = Depends(get_db)
):
    """Get a list of active users that the current user is allowed to tag."""
    query = db.query(models.User).filter(
        models.User.is_active == True,
        models.User.email != "googlecloudcron@manhattancomfort.com"
    )
    
    if role:
        query = query.filter(models.User.role == role)
    else:
        if current_user.role == "warehouse":
            query = query.filter(
                (models.User.role == "admin") | 
                ((models.User.role == "warehouse") & (models.User.warehouse_id == current_user.warehouse_id))
            )
        elif current_user.role == "vendor":
            query = query.filter(
                (models.User.role == "admin") | 
                (models.User.role == "office") | 
                ((models.User.role == "vendor") & (models.User.vendor_id.in_(current_user.effective_vendor_ids)))
            )
        
    users = query.all()
    return [UserMentionOut.model_validate(u) for u in users]


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(user_data: UserCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Create a new user account.
    
    - **email**: Valid email address (must be unique)
    - **password**: Plain text password (will be hashed before storing)
    - **first_name**: First name of the user
    - **last_name**: Last name of the user
    - **role**: User role (default: "user", can be "admin", "user", etc.)
    
    Note: This endpoint is public and doesn't require authentication.
    In production, you may want to add authentication or rate limiting.
    """
    # Convert email to lowercase for case insensitivity
    user_data.email = user_data.email.lower() if user_data.email else user_data.email
    
    # Check if user already exists
    existing_user = db.query(models.User).filter(
        models.User.email == user_data.email
    ).first()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Check vendor role requirements
    resolved_vendor_ids = []
    if user_data.role == "vendor":
        if user_data.vendor_ids:
            for raw_vid in user_data.vendor_ids:
                v_obj = resolve_vendor_helper(db, raw_vid)
                if not v_obj:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Vendor '{raw_vid}' not found"
                    )
                resolved_vendor_ids.append(str(v_obj.id))
        elif user_data.vendor_id:
            v_obj = resolve_vendor_helper(db, user_data.vendor_id)
            if not v_obj:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Vendor '{user_data.vendor_id}' not found"
                )
            resolved_vendor_ids.append(str(v_obj.id))
        else:
            # If vendor_id is not provided, create a new vendor using the provided fields
            v_name = user_data.vendor_name or f"{user_data.first_name} {user_data.last_name}"
            new_vendor = models.Vendor(
                name=v_name,
                country=user_data.country,
                phone=user_data.phone,
                payment_terms=user_data.payment_terms,
                container_lead_time_days=user_data.lead_time
            )
            db.add(new_vendor)
            db.commit()
            db.refresh(new_vendor)
            resolved_vendor_ids.append(str(new_vendor.id))

    primary_vendor_id = uuid.UUID(resolved_vendor_ids[0]) if resolved_vendor_ids else None
            
    # Check warehouse role requirements
    if user_data.role == "warehouse":
        if user_data.warehouse_id:
            warehouse_exists = db.query(models.Warehouse).filter(models.Warehouse.id == user_data.warehouse_id).first()
            if not warehouse_exists:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Warehouse not found"
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="warehouse_id is required for warehouse role"
            )
    
    # Hash the password
    hashed_password = auth_utils.hash_password(user_data.password)
    
    # Create new user
    full_name = f"{user_data.first_name} {user_data.last_name}".strip()
    new_user = models.User(
        email=user_data.email,
        hashed_password=hashed_password,
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        full_name=full_name,
        role=user_data.role,
        vendor_id=primary_vendor_id if user_data.role == "vendor" else None,
        vendor_ids=resolved_vendor_ids if user_data.role == "vendor" else [],
        warehouse_id=user_data.warehouse_id if user_data.role == "warehouse" else None,
        country=user_data.country if user_data.role == "vendor" else None,
        phone=user_data.phone if user_data.role == "vendor" else None,
        payment_terms=user_data.payment_terms if user_data.role == "vendor" else None,
        container_lead_time_days=user_data.lead_time if user_data.role == "vendor" else None,
        is_active=True
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # Log the activity
    log_activity(db, action="REGISTER", user_id=new_user.id, entity_type="USER", entity_id=str(new_user.id))

    # Send welcome email in background
    login_url = f"{settings.FRONTEND_ORIGIN}/login"
    background_tasks.add_task(
        send_welcome_email,
        email_to=user_data.email,
        password=user_data.password,
        login_link=login_url,
        first_name=user_data.first_name
    )
    
    # Send notification to admins in background
    admins = db.query(models.User).filter(models.User.role == "admin", models.User.notify_new_user == True).all()
    admin_emails = [a.email for a in admins if a.email]
    if admin_emails:
        background_tasks.add_task(
            send_admin_new_user_notification,
            admin_emails=admin_emails,
            new_user_name=full_name,
            new_user_email=user_data.email,
            new_user_role=user_data.role
        )
    
    return enrich_user_out(new_user, db)


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth_utils.get_current_user)
):
    """
    Get details of a specific user.
    Requires admin privileges or the user requesting their own details.
    """
    if current_user.role != "admin" and str(current_user.id) != user_id:
        raise HTTPException(status_code=403, detail="Not authorized. Admin or own account only.")

    user = db.query(models.User).options(
        joinedload(models.User.vendor),
        joinedload(models.User.warehouse)
    ).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return enrich_user_out(user, db)


@router.post("/users/{user_id}", response_model=UserOut)
@router.patch("/users/{user_id}", response_model=UserOut)
@router.put("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth_utils.get_current_user)
):
    """
    Update a user's details.
    Requires admin privileges.
    """
    if current_user.role != "admin" and str(current_user.id) != user_id:
        raise HTTPException(status_code=403, detail="Not authorized. Admin or own account only.")

    user = db.query(models.User).options(
        joinedload(models.User.vendor),
        joinedload(models.User.warehouse)
    ).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if user.email == "googlecloudcron@manhattancomfort.com":
        raise HTTPException(status_code=403, detail="This system account cannot be edited.")

    update_data = user_update.model_dump(exclude_unset=True)
    
    if current_user.role != "admin":
        # Non-admins cannot update sensitive fields
        restricted_keys = ["role", "vendor_id", "vendor_ids", "warehouse_id", "is_active", "container_lead_time_days", "payment_terms"]
        for key in restricted_keys:
            update_data.pop(key, None)
    
    # Validation for role-specific IDs
    if "role" in update_data:
        new_role = update_data["role"]
        if new_role == "vendor":
            v_ids = update_data.get("vendor_ids", user.vendor_ids)
            v_id = update_data.get("vendor_id", user.vendor_id)
            if not v_ids and not v_id:
                raise HTTPException(status_code=400, detail="vendor_id or vendor_ids is required for vendor role")
        elif new_role == "warehouse":
            w_id = update_data.get("warehouse_id", user.warehouse_id)
            if not w_id:
                raise HTTPException(status_code=400, detail="warehouse_id is required for warehouse role")

    # Handle multi-vendor or single vendor updates
    if "vendor_ids" in update_data:
        raw_vids = update_data.pop("vendor_ids")
        if raw_vids is not None:
            if isinstance(raw_vids, list):
                list_vids = raw_vids
            else:
                list_vids = [raw_vids]
            str_vids = []
            for raw_vid in list_vids:
                if not raw_vid:
                    continue
                v_obj = resolve_vendor_helper(db, raw_vid)
                if not v_obj:
                    raise HTTPException(status_code=400, detail=f"Vendor '{raw_vid}' not found")
                str_vids.append(str(v_obj.id))
            user.vendor_ids = str_vids
            user.vendor_id = uuid.UUID(str_vids[0]) if str_vids else None
        else:
            user.vendor_ids = []
            user.vendor_id = None
        # Remove vendor_id from update_data if present to avoid conflict
        update_data.pop("vendor_id", None)
    elif "vendor_id" in update_data:
        raw_vid = update_data.get("vendor_id")
        if raw_vid is not None:
            v_obj = resolve_vendor_helper(db, raw_vid)
            if not v_obj:
                raise HTTPException(status_code=400, detail=f"Vendor '{raw_vid}' not found")
            user.vendor_id = v_obj.id
            user.vendor_ids = [str(v_obj.id)]
        else:
            user.vendor_id = None
            user.vendor_ids = []

    if "password" in update_data:
        password = update_data.pop("password")
        if password:
            user.hashed_password = auth_utils.hash_password(password)

    for key, value in update_data.items():
        setattr(user, key, value)
        
    # Recalculate full name if first or last name changed
    if "first_name" in update_data or "last_name" in update_data:
        fname = user.first_name or ""
        lname = user.last_name or ""
        user.full_name = f"{fname} {lname}".strip()
        
    db.commit()
    db.refresh(user)
    
    return enrich_user_out(user, db)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth_utils.get_current_user)
):
    """
    Soft delete a user.
    Requires admin privileges.
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized. Admin only.")
        
    if str(current_user.id) == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if user.email == "googlecloudcron@manhattancomfort.com":
        raise HTTPException(status_code=403, detail="This system account cannot be deleted.")

    user.is_active = False
    db.commit()
    
    return None


@router.put("/update-password")
def update_password(
    request: UpdatePasswordRequest,
    current_user: models.User = Depends(auth_utils.get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update password for the currently logged-in user.
    """
    if request.password != request.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match"
        )
    
    # Hash the new password
    hashed_password = auth_utils.hash_password(request.password)
    current_user.hashed_password = hashed_password
    
    db.commit()
    
    return {"message": "Password updated successfully"}
