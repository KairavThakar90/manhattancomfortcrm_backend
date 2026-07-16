from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth.repository import AuthRepository
from app.auth.security import (
    verify_password,
    create_access_token,
)


class AuthService:

    @staticmethod
    def login(
        db: Session,
        email: str,
        password: str,
    ):

        user = AuthRepository.get_user_by_email(
            db,
            email,
        )

        if not user:
            raise HTTPException(
                status_code=401,
                detail="Invalid email or password",
            )

        if not verify_password(
            password,
            user.password_hash,
        ):
            raise HTTPException(
                status_code=401,
                detail="Invalid email or password",
            )

        token = create_access_token(
            {
                "sub": user.email,
                "role": user.role,
            }
        )

        return {
            "access_token": token,
            "token_type": "Bearer",
            "user": user,
        }