import uuid
from typing import Optional, List, Union
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.database import get_db
from app.auth import get_current_user
from app import models
from app.schemas import (
    ProductCreate, ProductUpdate, ProductOut, ProductBulkCreate, PaginatedResponse
)

router = APIRouter(prefix="/products", tags=["Products / SKUs"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=PaginatedResponse)
def list_products(
    vendor_id: Optional[str] = Query(None, description="Filter by specific Vendor UUID"),
    search: Optional[str] = Query(None, description="Search by SKU or product name"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    page: Optional[int] = Query(None, ge=1, description="Page number"),
    page_size: Optional[int] = Query(None, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    List all product SKUs with optional filtering by vendor and search query.
    - Vendor role users are automatically scoped to only their assigned vendors.
    - Admin / Office / Warehouse users can filter by any vendor or view all.
    """
    query = db.query(models.Product).options(joinedload(models.Product.vendor))

    # 1. Role-based scoping for Vendor users
    if current_user.role == "vendor":
        effective_vids = current_user.effective_vendor_ids
        if not effective_vids:
            return PaginatedResponse(total=0, page=page or 1, page_size=page_size or 25, results=[])
        query = query.filter(models.Product.vendor_id.in_(effective_vids))

    # 2. Filter by vendor_id parameter
    if vendor_id:
        try:
            v_uuid = uuid.UUID(str(vendor_id).strip())
            # If vendor role tries to filter by vendor outside their assigned list, deny or return empty
            if current_user.role == "vendor" and v_uuid not in current_user.effective_vendor_ids:
                return PaginatedResponse(total=0, page=page or 1, page_size=page_size or 25, results=[])
            query = query.filter(models.Product.vendor_id == v_uuid)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid vendor_id format. Must be a valid UUID.")

    # 3. Active status filter
    if is_active is not None:
        query = query.filter(models.Product.is_active == is_active)

    # 4. Search query
    if search:
        search_pattern = f"%{search.strip()}%"
        query = query.filter(
            or_(
                models.Product.sku.ilike(search_pattern),
                models.Product.product_name.ilike(search_pattern)
            )
        )

    # Order by SKU ascending
    query = query.order_by(models.Product.sku.asc())

    total = query.count()

    if page is not None and page_size is not None:
        products = query.offset((page - 1) * page_size).limit(page_size).all()
        results = [ProductOut.model_validate(p).model_dump(mode='python') for p in products]
        return PaginatedResponse(total=total, page=page, page_size=page_size, results=results)
    elif page is not None and page_size is None:
        p_size = 25
        products = query.offset((page - 1) * p_size).limit(p_size).all()
        results = [ProductOut.model_validate(p).model_dump(mode='python') for p in products]
        return PaginatedResponse(total=total, page=page, page_size=p_size, results=results)
    elif page is None and page_size is not None:
        products = query.limit(page_size).all()
        results = [ProductOut.model_validate(p).model_dump(mode='python') for p in products]
        return PaginatedResponse(total=total, page=1, page_size=page_size, results=results)
    else:
        # Neither page nor page_size provided: return ALL records
        products = query.all()
        results = [ProductOut.model_validate(p).model_dump(mode='python') for p in products]
        return PaginatedResponse(total=total, page=1, page_size=total if total > 0 else 1, results=results)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get single product SKU details."""
    try:
        p_uuid = uuid.UUID(str(product_id).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid product ID format. Must be a valid UUID.")

    product = db.query(models.Product).options(joinedload(models.Product.vendor)).filter(models.Product.id == p_uuid).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product SKU not found")

    # Vendor scoping check
    if current_user.role == "vendor" and product.vendor_id not in current_user.effective_vendor_ids:
        raise HTTPException(status_code=403, detail="Not authorized to access products for this vendor.")

    return product


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Create a new Product SKU."""
    # Check vendor assignment permissions
    v_uuid = None
    if payload.vendor_id:
        try:
            v_uuid = uuid.UUID(str(payload.vendor_id).strip())
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid vendor_id format. Must be a valid UUID.")

        vendor = db.query(models.Vendor).filter(models.Vendor.id == v_uuid).first()
        if not vendor:
            raise HTTPException(status_code=404, detail="Assigned vendor not found")

        if current_user.role == "vendor" and v_uuid not in current_user.effective_vendor_ids:
            raise HTTPException(status_code=403, detail="Cannot assign SKU to an unauthorized vendor.")

    elif current_user.role == "vendor":
        # Default to vendor's primary vendor ID if available
        v_uuid = current_user.vendor_id

    product = models.Product(
        sku=payload.sku.strip(),
        product_name=payload.product_name,
        vendor_id=v_uuid,
        date=payload.date or datetime.utcnow(),
        price=payload.price,
        is_active=payload.is_active if payload.is_active is not None else True
    )
    db.add(product)
    db.commit()
    db.refresh(product)

    # Automatically register SKU in SellerCloud Catalog if vendor has a SellerCloud ID
    if v_uuid:
        vendor_obj = db.query(models.Vendor).filter(models.Vendor.id == v_uuid).first()
        if vendor_obj and vendor_obj.sellercloud_vendor_id:
            sc_company_id = 255
            if payload.company_id:
                c_raw = str(payload.company_id).strip()
                try:
                    c_uuid = uuid.UUID(c_raw)
                    comp = db.query(models.Company).filter(models.Company.id == c_uuid).first()
                    if comp and comp.sellercloud_company_id:
                        sc_company_id = comp.sellercloud_company_id
                except ValueError:
                    if c_raw.isdigit():
                        sc_company_id = int(c_raw)
                    else:
                        comp = db.query(models.Company).filter(models.Company.name.ilike(c_raw)).first()
                        if comp and comp.sellercloud_company_id:
                            sc_company_id = comp.sellercloud_company_id

            try:
                from app.services.sellercloud_client import sellercloud_client
                sellercloud_client.create_product(
                    sku=product.sku,
                    product_name=product.product_name or product.sku,
                    company_id=sc_company_id,
                    vendor_id=vendor_obj.sellercloud_vendor_id,
                    site_cost=product.price
                )
            except Exception as exc:
                print(f"[create_product] Notice: SellerCloud product sync: {exc}")

    return product


@router.post("/bulk", response_model=List[ProductOut], status_code=status.HTTP_201_CREATED)
def bulk_create_products(
    payload: ProductBulkCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Bulk create / import product SKUs."""
    created = []
    for item in payload.products:
        v_uuid = None
        if item.vendor_id:
            try:
                v_uuid = uuid.UUID(str(item.vendor_id).strip())
            except (ValueError, AttributeError):
                continue
            if current_user.role == "vendor" and v_uuid not in current_user.effective_vendor_ids:
                continue
        elif current_user.role == "vendor":
            v_uuid = current_user.vendor_id

        prod = models.Product(
            sku=item.sku.strip(),
            product_name=item.product_name,
            vendor_id=v_uuid,
            date=item.date or datetime.utcnow(),
            price=item.price,
            is_active=item.is_active if item.is_active is not None else True
        )
        db.add(prod)
        created.append(prod)

    db.commit()
    for prod in created:
        db.refresh(prod)
    return created


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: str,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Update a product SKU."""
    try:
        p_uuid = uuid.UUID(str(product_id).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid product ID format. Must be a valid UUID.")

    product = db.query(models.Product).filter(models.Product.id == p_uuid).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product SKU not found")

    if current_user.role == "vendor" and product.vendor_id not in current_user.effective_vendor_ids:
        raise HTTPException(status_code=403, detail="Not authorized to edit products for this vendor.")

    update_data = payload.model_dump(exclude_unset=True)

    if "vendor_id" in update_data and update_data["vendor_id"]:
        try:
            v_uuid = uuid.UUID(str(update_data["vendor_id"]).strip())
            if current_user.role == "vendor" and v_uuid not in current_user.effective_vendor_ids:
                raise HTTPException(status_code=403, detail="Cannot assign SKU to an unauthorized vendor.")
            update_data["vendor_id"] = v_uuid
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid vendor_id format. Must be a valid UUID.")

    for key, value in update_data.items():
        setattr(product, key, value)

    product.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Delete a product SKU (Admin only)."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can delete products.")

    try:
        p_uuid = uuid.UUID(str(product_id).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid product ID format. Must be a valid UUID.")

    product = db.query(models.Product).filter(models.Product.id == p_uuid).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product SKU not found")

    db.delete(product)
    db.commit()
    return None
