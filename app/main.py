from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, companies, customers, purchase_orders

app = FastAPI(title="Manhattan Comfort CRM API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(companies.router)
app.include_router(customers.router)
app.include_router(purchase_orders.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
