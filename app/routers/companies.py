from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import CompanyOut, PaginatedResponse, SyncResponse
from app.services.sync_service import sync_companies

router = APIRouter(prefix="/companies", tags=["Companies"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=PaginatedResponse)
def list_companies(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(models.Company)
    total = q.count()
    rows = q.order_by(models.Company.name).offset((page - 1) * page_size).limit(page_size).all()
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[CompanyOut.model_validate(r) for r in rows],
    )


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(company_id: str, db: Session = Depends(get_db)):
    company = db.query(models.Company).filter(models.Company.id == company_id).first()
    return company


@router.post("/sync", response_model=SyncResponse)
def trigger_sync(db: Session = Depends(get_db)):
    """Pulls latest Companies from SellerCloud into Neon."""
    count = sync_companies(db)
    return SyncResponse(entity_type="companies", status="success", records_synced=count)
