import requests

from app.sellercloud.auth import get_access_token
from app.core.config import SELLERCLOUD_BASE_URL


def get_purchase_orders(
    page_number: int = 1,
    page_size: int = 50,
):
    token = get_access_token()

    url = (
        f"{SELLERCLOUD_BASE_URL}"
        f"/rest/api/PurchaseOrders/GetAllByView"
    )

    params = {
        "viewID": 25,
        "pageNumber": page_number,
        "pageSize": page_size,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=60,
    )

    print(f"Status Code: {response.status_code}")

    response.raise_for_status()

    return response.json()