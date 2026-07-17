from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import CustomerOut, PaginatedResponse

router = APIRouter(prefix="/customers", tags=["Customers"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=PaginatedResponse)
def list_customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    company_id: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Customer)
    if company_id:
        q = q.filter(models.Customer.company_id == company_id)
    total = q.count()
    rows = q.order_by(models.Customer.last_name).offset((page - 1) * page_size).limit(page_size).all()
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        results=[CustomerOut.model_validate(r) for r in rows],
    )


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, db: Session = Depends(get_db)):
    return db.query(models.Customer).filter(models.Customer.id == customer_id).first()
