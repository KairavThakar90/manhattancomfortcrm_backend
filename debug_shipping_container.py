"""
One-off debug script: fetches real ShippingContainers data for one PO + SKU so
we can confirm the actual field names before wiring up automatic sync (same
reason debug_po_detail.py exists - the PurchaseOrders detail endpoint's real
shape didn't match what we guessed from the Apps Script).

Run: python debug_shipping_container.py <po_id> <sku>
e.g. python debug_shipping_container.py 12247 DC073-CY-1682-2-LKHC390-18

If you don't pass args, this grabs a real PO + one of its SKUs from Neon
automatically (requires you've already run a successful purchase-orders sync).
"""
import sys
import json

from app.services.sellercloud_client import sellercloud_client
from app.database import SessionLocal
from app import models

po_id = sys.argv[1] if len(sys.argv) > 1 else None
sku = sys.argv[2] if len(sys.argv) > 2 else None

if not po_id or not sku:
    print("No po_id/sku given - grabbing one from Neon...")
    db = SessionLocal()
    item = (
        db.query(models.PurchaseOrderItem)
        .join(models.PurchaseOrder)
        .filter(models.PurchaseOrderItem.sku.isnot(None))
        .first()
    )
    if not item:
        db.close()
        print("No purchase_order_items with a SKU found in Neon. Run a PO sync first, "
              "or pass po_id and sku manually: python debug_shipping_container.py 12247 SOME-SKU")
        sys.exit(1)
    po_id = item.purchase_order.sellercloud_po_id  # read while session is still open
    sku = item.sku
    db.close()
    print(f"Using PO {po_id}, SKU {sku}")

print(f"\n--- Step 1: GET /ShippingContainers?model.poIds={po_id}&model.productIds={sku} ---")
containers_response = sellercloud_client.get_containers_for_po_product(po_id, sku)
print(json.dumps(containers_response, indent=2, default=str))

# Try to find a container ID in whatever shape the response actually is -
# this part is intentionally exploratory since we don't know the shape yet.
container_id = None
if isinstance(containers_response, list) and containers_response:
    container_id = containers_response[0].get("ID") or containers_response[0].get("Id")
elif isinstance(containers_response, dict):
    items = containers_response.get("Items") or containers_response.get("Containers") or []
    if items:
        container_id = items[0].get("ID") or items[0].get("Id")

if not container_id:
    print("\nCould not auto-detect a container ID from the response above - "
          "paste this output back and we'll figure out the right key together.")
    sys.exit(0)

print(f"\n--- Step 2: GET /ShippingContainers/{container_id} ---")
container_detail = sellercloud_client.get_container(container_id)
print(json.dumps(container_detail, indent=2, default=str))
