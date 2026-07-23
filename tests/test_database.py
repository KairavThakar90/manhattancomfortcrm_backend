from app.database import normalize_database_url


def test_localhost_database_url_does_not_force_sslmode():
    url = "postgresql://user:pass@localhost:5432/manhattancomfortcrm"
    assert normalize_database_url(url) == url


def test_remote_database_url_adds_sslmode_require():
    url = "postgresql://user:pass@db.example.com:5432/manhattancomfortcrm"
    assert normalize_database_url(url) == "postgresql://user:pass@db.example.com:5432/manhattancomfortcrm?sslmode=require"
