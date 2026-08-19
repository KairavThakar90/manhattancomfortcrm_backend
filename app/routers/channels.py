from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import models, schemas
from app.database import get_db

router = APIRouter(
    prefix="/api/v1/channels",
    tags=["channels"]
)

@router.get("/", response_model=List[schemas.ChannelOut])
def get_channels(db: Session = Depends(get_db)):
    """
    Retrieve a list of all distinct channels associated with purchase orders.
    """
    channels = db.query(models.Channel).order_by(models.Channel.name).all()
    return channels
