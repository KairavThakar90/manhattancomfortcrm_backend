"""
One-off script to create a login user for your app.
Run: python create_user.py
"""
from app.database import SessionLocal
from app import models
from app.auth import hash_password

db = SessionLocal()

email = input("Email: ").strip()
password = input("Password: ").strip()
full_name = input("Full name: ").strip()

existing = db.query(models.User).filter(models.User.email == email).first()
if existing:
    print("User already exists.")
else:
    user = models.User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role="admin",
    )
    db.add(user)
    db.commit()
    print(f"Created user {email}")

db.close()
