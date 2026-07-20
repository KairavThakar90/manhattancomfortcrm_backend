"""
One-off debug script: fetches ONE PO's full detail from SellerCloud and prints
its exact top-level keys + a sample of values, so we can see the real field
names instead of guessing from the list-endpoint shape.

Run: python debug_po_detail.py <a_real_po_id>
e.g. python debug_po_detail.py 123456

If you don't know a PO ID, leave it blank and this will grab one automatically
from the GetAllByView list first.
"""
import sys
import json

from app.services.sellercloud_client import sellercloud_client

po_id = sys.argv[1] if len(sys.argv) > 1 else None

if not po_id:
    print("No PO ID given - fetching one from GetAllByView...")
    listing = sellercloud_client.get_purchase_orders_by_view(page_number=1, page_size=1)
    items = listing.get("Items") or []
    if not items:
        print("No POs found in the view. Pass a PO ID manually: python debug_po_detail.py 123456")
        sys.exit(1)
    po_id = items[0].get("ID")
    print(f"Using PO ID {po_id} from the list endpoint.")
    print("\n--- LIST endpoint row (for comparison) ---")
    print(json.dumps(items[0], indent=2, default=str)[:2000])

print(f"\n--- DETAIL endpoint response for PO {po_id} ---")
detail = sellercloud_client.get_purchase_order(po_id)

print("\nTop-level keys in the detail response:")
print(list(detail.keys()))

for section in ["Statuses", "TotalInfo", "CustomColumns", "RelatedItems"]:
    print(f"\n--- detail['{section}'] (full) ---")
    print(json.dumps(detail.get(section), indent=2, default=str))

items = detail.get("Items") or []
if items:
    print("\n--- detail['Items'][0] (full, one complete line item) ---")
    print(json.dumps(items[0], indent=2, default=str))
else:
    print("\nNo Items found on this PO.")
