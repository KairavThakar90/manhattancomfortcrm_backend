from fastapi import FastAPI

from app.core.database import Base, engine
from app.users.model import User
from app.auth.router import router as auth_router
from app.purchase_orders.model import PurchaseOrder
from app.purchase_orders.router import router as purchase_order_router

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Supply Chain CRM API",
    version="1.0.0"
)

app.include_router(auth_router)
app.include_router(purchase_order_router)


@app.get("/")
def root():
    return {
        "message": "Supply Chain CRM Backend Running"
    }