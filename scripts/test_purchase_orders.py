import json

from app.sellercloud.purchase_orders import get_purchase_orders

data = get_purchase_orders()

with open("purchase_orders.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

print("Purchase Orders saved to purchase_orders.json")