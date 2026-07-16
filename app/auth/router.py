from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.schema import LoginRequest
from app.auth.service import AuthService
from app.core.database import get_db

router = APIRouter(
    prefix="/api/v1/auth",
    tags=["Authentication"],
)


@router.post("/login")
def login(
    request: LoginRequest,
    db: Session = Depends(get_db),
):
    return AuthService.login(
        db=db,
        email=request.email,
        password=request.password,
    )