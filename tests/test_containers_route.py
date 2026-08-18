from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.main import app


def test_containers_list_endpoint_exists():
    app.dependency_overrides[get_current_user] = lambda: object()
    client = TestClient(app)
    response = client.get('/api/v1/containers/', params={'page': 1, 'page_size': 25})
    assert response.status_code == 200
    app.dependency_overrides.clear()
