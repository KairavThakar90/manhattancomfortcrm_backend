from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, companies, customers, vendors, purchase_orders

app = FastAPI(title="Manhattan Comfort CRM API", version="1.0.0")

# CORS configuration for production and development
# Allows requests from Vercel deployments and localhost
origins = [
    settings.FRONTEND_ORIGIN,
    "http://localhost:3000",
    "http://localhost:3001",
    "https://manhattancomfortcrm.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_origin_regex=r"https://manhattancomfortcrm.*\.vercel\.app",  # Allow all Vercel preview deployments
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(companies.router, prefix="/api/v1")
app.include_router(customers.router, prefix="/api/v1")
app.include_router(vendors.router, prefix="/api/v1")
app.include_router(purchase_orders.router, prefix="/api/v1")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/v1/health")
def health_check_v1():
    return {"status": "ok", "version": "v1"}
