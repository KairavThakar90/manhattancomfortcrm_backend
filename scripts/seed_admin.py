from app.core.database import SessionLocal
from app.users.model import User
from app.auth.security import hash_password

db = SessionLocal()

try:
    existing_user = (
        db.query(User)
        .filter(User.email == "admin@manhattancomfort.com")
        .first()
    )

    if existing_user:
        print("✅ Admin user already exists.")

    else:
        admin = User(
            first_name="Admin",
            last_name="User",
            email="admin@manhattancomfort.com",
            password_hash=hash_password("Admin@123"),
            role="Admin",
            is_active=True,
        )

        db.add(admin)
        db.commit()

        print("✅ Admin user created successfully!")

finally:
    db.close()