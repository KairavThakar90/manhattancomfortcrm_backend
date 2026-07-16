from sqlalchemy.orm import Session

from app.users.model import User


class AuthRepository:

    @staticmethod
    def get_user_by_email(db: Session, email: str):
        return (
            db.query(User)
            .filter(User.email == email)
            .first()
        )