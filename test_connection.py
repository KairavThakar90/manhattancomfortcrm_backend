"""
Quick standalone check that DATABASE_URL in .env actually connects to Neon.
Run: python test_connection.py
"""
from sqlalchemy import text
from app.database import engine

try:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version();"))
        version = result.scalar()
        print("✅ Connected to Neon successfully.")
        print(f"   Postgres version: {version}")

        tables = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name;"
        )).fetchall()
        print(f"   Tables found ({len(tables)}): {', '.join(t[0] for t in tables)}")

except Exception as e:
    print("❌ Connection failed.")
    print(f"   Error: {e}")
