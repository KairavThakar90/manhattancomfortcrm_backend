from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.database import get_db
from app import auth as auth_utils
from app.schemas import Token, UserOut

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    OAuth2 "password" flow: send as x-www-form-urlencoded with
    fields 'username' (= email) and 'password'.
    This also makes Swagger's Authorize button work out of the box.
    """
    user = auth_utils.authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    access_token = auth_utils.create_access_token(data={"sub": str(user.id)})
    return Token(access_token=access_token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def read_current_user(current_user=Depends(auth_utils.get_current_user)):
    return current_user
