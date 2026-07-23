from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings


def normalize_database_url(database_url: str) -> str:
    """Normalize Postgres URLs for SQLAlchemy/psycopg2.

    - Rewrite deprecated postgres:// scheme to postgresql://
    - Ensure sslmode=require for managed Postgres providers like Supabase
      when it is not already present.
    """
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    parsed = urlparse(database_url)
    if parsed.scheme.startswith("postgres"):
        # If credentials contain characters like '@', ensure the password is
        # URL-encoded so SQLAlchemy/psycopg2 parse host/port correctly.
        if parsed.password is not None:
            user_info = parsed.username or ""
            password = quote(parsed.password, safe="")
            database_url = urlunparse(parsed._replace(netloc=f"{user_info}:{password}@{parsed.hostname or ''}{f':{parsed.port}' if parsed.port else ''}"))
            parsed = urlparse(database_url)

        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if "sslmode" not in query:
            query["sslmode"] = "require"
            database_url = urlunparse(parsed._replace(query=urlencode(query)))

    return database_url


engine = create_engine(
    normalize_database_url(settings.DATABASE_URL),
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
