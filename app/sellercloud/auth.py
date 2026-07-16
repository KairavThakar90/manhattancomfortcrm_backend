import requests

from app.core.config import (
    SELLERCLOUD_BASE_URL,
    SELLERCLOUD_USERNAME,
    SELLERCLOUD_PASSWORD,
)


def get_access_token():
    url = f"{SELLERCLOUD_BASE_URL}/rest/api/token"

    payload = {
        "Username": SELLERCLOUD_USERNAME,
        "Password": SELLERCLOUD_PASSWORD,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=30,
    )

    print(f"Request URL: {url}")
    print(f"Status Code: {response.status_code}")

    response.raise_for_status()

    data = response.json()

    return data["access_token"]