from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app import auth as auth_utils
from app import models
from app.schemas import Token, RefreshTokenRequest, LogoutResponse, UserOut

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    OAuth2 "password" flow: send as x-www-form-urlencoded with
    fields 'username' (= email) and 'password'.
    This also makes Swagger's Authorize button work out of the box.
    
    Returns both access_token (short-lived) and refresh_token (long-lived).
    """
    user = auth_utils.authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    access_token = auth_utils.create_access_token(data={"sub": str(user.id)})
    refresh_token = auth_utils.create_refresh_token(data={"sub": str(user.id)})
    
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
