from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app import auth as auth_utils
from app import models
from app.services.email_service import send_welcome_email
from app.services.activity_service import log_activity
from app.config import settings
from app.schemas import Token, RefreshTokenRequest, LogoutResponse, UserOut, UserCreate, UpdatePasswordRequest

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    OAuth2 "password" flow: send as x-www-form-urlencoded with
    fields 'username' (= email) and 'password'.
    This also makes Swagger's Authorize button work out of the box.
    
    Returns both access_token (short-lived) and refresh_token (long-lived).
    """
    # Convert email to lowercase to ensure case insensitivity
    email = form_data.username.lower() if form_data.username else form_data.username
    user = auth_utils.authenticate_user(db, email, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

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
def read_current_user(current_user=Depends(auth_utils.get_current_user)):
    """Get current authenticated user information."""
    return current_user


@router.get("/users", response_model=list[UserOut])
def get_all_users(
    current_user: models.User = Depends(auth_utils.get_current_user),
    db: Session = Depends(get_db)
):
    """Get a list of all active users, for tagging/mentioning in comments."""
    users = db.query(models.User).filter(models.User.is_active == True).all()
    return [UserOut.model_validate(u) for u in users]


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
    if user_data.role == "vendor":
        if user_data.vendor_id:
            vendor_exists = db.query(models.Vendor).filter(models.Vendor.id == user_data.vendor_id).first()
            if not vendor_exists:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Vendor not found"
                )
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
            user_data.vendor_id = new_vendor.id
    
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
        vendor_id=user_data.vendor_id if user_data.role == "vendor" else None,
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
    
    return UserOut.model_validate(new_user)


@router.put("/update-password")
@router.post("/update-password")
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
