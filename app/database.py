from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

# Neon works fine with the plain psycopg2 driver + sslmode=require in the URL -
# no scheme rewriting needed, "postgresql://" is psycopg2's default dialect.
# pool_pre_ping avoids "server closed the connection" errors after idle time,
# which Neon's autosuspend can trigger.
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
