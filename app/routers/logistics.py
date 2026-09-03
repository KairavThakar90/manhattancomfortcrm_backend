from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import uuid

from app import models, schemas
from app.database import get_db
from app.auth import get_current_user

router = APIRouter(prefix="/logistics", tags=["Logistics"])

@router.get("", response_model=List[schemas.LogisticsCompanyOut])
def get_logistics_companies(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    return db.query(models.LogisticsCompany).order_by(models.LogisticsCompany.name.asc()).all()

@router.post("", response_model=schemas.LogisticsCompanyOut)
def create_logistics_company(
    logistics: schemas.LogisticsCompanyCreate, 
    db: Session = Depends(get_db), 
    current_user=Depends(get_current_user)
):
    existing = db.query(models.LogisticsCompany).filter(models.LogisticsCompany.name == logistics.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Logistics company with this name already exists")
        
    new_company = models.LogisticsCompany(**logistics.dict())
    db.add(new_company)
    db.commit()
    db.refresh(new_company)
    return new_company

@router.put("/{company_id}", response_model=schemas.LogisticsCompanyOut)
def update_logistics_company(
    company_id: uuid.UUID, 
    logistics: schemas.LogisticsCompanyUpdate, 
    db: Session = Depends(get_db), 
    current_user=Depends(get_current_user)
):
    company = db.query(models.LogisticsCompany).filter(models.LogisticsCompany.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Logistics company not found")
        
    update_data = logistics.dict(exclude_unset=True)
    
    if "name" in update_data:
        existing = db.query(models.LogisticsCompany).filter(
            models.LogisticsCompany.name == update_data["name"],
            models.LogisticsCompany.id != company_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Another logistics company with this name already exists")
            
    for key, value in update_data.items():
        setattr(company, key, value)
        
    db.commit()
    db.refresh(company)
    return company

@router.delete("/{company_id}")
def delete_logistics_company(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    company = db.query(models.LogisticsCompany).filter(models.LogisticsCompany.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Logistics company not found")

    db.delete(company)
    db.commit()
    return {"success": True, "message": "Logistics company deleted successfully"}
