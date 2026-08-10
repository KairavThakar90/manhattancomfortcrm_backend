"""
Thin wrapper around the SellerCloud REST API.

Flow (per SellerCloud docs):
1. POST {base}/api/token  with username/password -> returns access_token
2. Every subsequent call sends: Authorization: Bearer {access_token}
3. Token expires - we cache it in memory and re-fetch a bit before expiry.

NOTE: Confirm exact field/endpoint names against your own Swagger UI
(https://tt.api.sellercloud.com/rest/swagger/ui/) since custom fields and
some route names can differ slightly per SellerCloud tenant/version.
"""
import time
from typing import Optional

import httpx

from app.config import settings


class SellerCloudClient:
    def __init__(self):
        base_url = settings.SELLERCLOUD_BASE_URL.rstrip("/")
        
        # Validate that base_url has protocol
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"SELLERCLOUD_BASE_URL must start with 'http://' or 'https://'. "
                f"Got: '{base_url}'. "
                f"Expected format: 'https://cd.api.sellercloud.com/rest'"
            )
        
        self.base_url = base_url
        self.username = settings.SELLERCLOUD_USERNAME
        self.password = settings.SELLERCLOUD_PASSWORD
        self._token: Optional[str] = None
        self._token_expires_at: float = 0

    # ---------------- Auth ----------------
    def _login(self) -> str:
        url = f"{self.base_url}/api/token"
        resp = httpx.post(
            url,
            json={"Username": self.username, "Password": self.password},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token") or data.get("Token") or data.get("token")
        if not token:
            raise RuntimeError(f"SellerCloud login did not return a token: {data}")

        # SellerCloud tokens commonly last ~24h; refresh a bit early to be safe.
        expires_in = data.get("expires_in", 60 * 60 * 20)
        self._token = token
        self._token_expires_at = time.time() + int(expires_in) - 60
        return token

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._token_expires_at:
            self._login()
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, retry: bool = True, **kwargs) -> httpx.Response:
        url = f"{self.base_url}{path}"
        resp = httpx.request(method, url, headers=self._headers(), timeout=60, **kwargs)

        # Token might have expired server-side even though our clock says it's fine.
        if resp.status_code == 401 and retry:
            self._token = None
            return self._request(method, path, retry=False, **kwargs)

        resp.raise_for_status()
        return resp

    # ---------------- Companies ----------------
    def get_companies(self, page_number: int = 1, page_size: int = 100) -> dict:
        resp = self._request(
            "GET",
            "/api/Companies",
            params={"pageNumber": page_number, "pageSize": page_size},
        )
        return resp.json()

    # ---------------- Vendors ----------------
    def get_vendors(self, page_number: int = 1, page_size: int = 100) -> dict:
        resp = self._request(
            "GET",
            "/api/Vendors",
            params={"pageNumber": page_number, "pageSize": page_size},
        )
        return resp.json()
    
    # ---------------- Warehouses ----------------
    def get_warehouses(self) -> dict:
        resp = self._request(
            "GET",
            "/api/Warehouses",
        )
        return resp.json()

    def get_vendor(self, vendor_id: int) -> dict:
        """Get full vendor detail by ID."""
        resp = self._request("GET", f"/api/Vendors/{vendor_id}")
        return resp.json()

    # ---------------- Customers ----------------
    def get_customers(self, page_number: int = 1, page_size: int = 100) -> dict:
        """Fetch a page of customers."""
        resp = self._request("GET", "/api/Customers", params={"pageNumber": page_number, "pageSize": page_size})
        return resp.json()
        
    # ---------------- Orders ----------------
    def get_order(self, order_id: int) -> dict:
        """Fetch a specific order."""
        resp = self._request("GET", f"/api/Orders/{order_id}")
        return resp.json()

    # ---------------- Warehouses ----------------
    def get_warehouses(self) -> dict:
        """Fetch all warehouses."""
        resp = self._request("GET", "/api/Warehouses", params={"pageSize": 100})
        return resp.json()

    # ---------------- Purchase Orders ----------------
    def get_purchase_orders_by_view(
        self, view_id: int = None, page_number: int = 1, page_size: int = 50
    ) -> dict:
        """
        Mirrors the confirmed-working Apps Script call:
        GET /api/PurchaseOrders/GetAllByView?viewID={id}&pageNumber={p}&pageSize={s}

        This is a SAVED VIEW/FILTER in SellerCloud (view 25 in your script), so it
        only returns POs matching whatever filter that view applies - not all POs.
        The list response items do NOT include QtyInContainer per item; use
        get_purchase_order() for that.
        """
        resp = self._request(
            "GET",
            "/api/PurchaseOrders/GetAllByView",
            params={
                "viewID": view_id or 25,
                "pageNumber": page_number,
                "pageSize": page_size,
            },
        )
        return resp.json()

    def get_purchase_order(self, po_id: int) -> dict:
        """Full PO detail, including Items[].QtyInContainer."""
        resp = self._request("GET", f"/api/PurchaseOrders/{po_id}")
        return resp.json()

    # Alias used by the single-PO sync endpoint in the purchase_orders router
    def get_purchase_order_detail(self, po_id: int) -> dict:
        return self.get_purchase_order(po_id)

    def get(self, path: str, **params) -> dict:
        """Generic GET helper (used by container sync endpoint and others)."""
        resp = self._request("GET", path, params=params or None)
        return resp.json()

    # ---------------- Shipping Containers ----------------
    def get_containers_for_po_product(self, po_id: int, product_id: str) -> dict:
        """
        Step 1 of container lookup: find container IDs for a given PO + SKU.
        GET /api/ShippingContainers?model.poIds={po_id}&model.productIds={product_id}
        """
        resp = self._request(
            "GET",
            "/api/ShippingContainers",
            params={"model.poIds": po_id, "model.productIds": product_id},
        )
        return resp.json()

    def get_container(self, container_id: int) -> dict:
        """Step 2: full container detail (name + per-product quantities)."""
        resp = self._request("GET", f"/api/ShippingContainers/{container_id}")
        return resp.json()

    def create_shipping_container(self, payload: dict) -> dict:
        """
        Create a new shipping container in SellerCloud.
        POST /api/ShippingContainers
        
        NOTE: Per the SC Swagger spec, this endpoint only accepts:
          ContainerName, ReceivingWarehouseID, ShippingStatus, ShippedOn, EstimatedArrivalDate
        Items CANNOT be passed here — use add_items_to_container() after creation.

        Returns the raw SC response. The new container integer ID is in the
        response body directly (SC returns a plain integer, not JSON).
        """
        # Strip Items out — SC doesn't accept them in the create payload
        create_payload = {
            "ContainerName": payload.get("ContainerName"),
            "EstimatedArrivalDate": payload.get("EstimatedArrivalDate"),
            "ShippedOn": payload.get("ReceivedDate"),  # map our ReceivedDate to ShippedOn
            "ShippingStatus": 1,  # 1 = NotArrived (default for new containers)
        }
        # Remove None values
        create_payload = {k: v for k, v in create_payload.items() if v is not None}

        resp = self._request("POST", "/api/ShippingContainers", json=create_payload)
        self._last_create_status = resp.status_code
        self._last_create_raw = resp.text

        try:
            parsed = resp.json()
            # SC may return a plain integer (the new container ID)
            if isinstance(parsed, int):
                return {"ID": parsed}
            return parsed
        except Exception:
            raw = resp.text.strip()
            if raw.isdigit():
                return {"ID": int(raw)}
            return {"_raw_response": raw}

    def update_shipping_container(self, container_id: int, payload: dict) -> dict:
        """
        Update an existing shipping container in SellerCloud.
        PUT /api/ShippingContainers/{id}
        """
        update_payload = {
            "ContainerName": payload.get("ContainerName"),
            "EstimatedArrivalDate": payload.get("EstimatedArrivalDate"),
            "ShippedOn": payload.get("ReceivedDate"),
            "ShippingStatus": payload.get("ShippingStatus", 1),
        }
        # Remove None values
        update_payload = {k: v for k, v in update_payload.items() if v is not None}
        
        resp = self._request("PUT", f"/api/ShippingContainers/{container_id}", json=update_payload)
        
        # SC returns empty body on success
        if resp.status_code == 200:
            return {"success": True}
            
        try:
            return resp.json()
        except Exception:
            return {"_raw_response": resp.text}

    def add_items_to_container(self, container_id: int, items: list) -> None:
        """
        Add items to an existing container.
        POST /api/ShippingContainers/{id}/Items

        items: list of dicts with keys:
          - PurchaseOrderID (int, required)
          - PurchaseOrderItemID (int, required)
          - Qty (int, required)
        """
        payload = {"Items": items}
        self._request("POST", f"/api/ShippingContainers/{container_id}/Items", json=payload)

    def search_containers_by_name(self, name: str) -> dict:
        """
        Search containers by name — used as fallback to recover the SC ID
        when the create response doesn't include it explicitly.
        GET /api/ShippingContainers?model.containerNames={name}
        """
        resp = self._request(
            "GET",
            "/api/ShippingContainers",
            params={"model.containerNames": name},
        )
        return resp.json()


sellercloud_client = SellerCloudClient()

