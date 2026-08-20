import re
import requests
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session
from dateutil import parser as date_parser

from app.config import settings
from app import models


def create_allways_session() -> Optional[requests.Session]:
    """
    Authenticates with AllWays USA web portal and returns an active requests.Session.
    """
    if not settings.ALLWAYS_LOGIN_EMAIL or not settings.ALLWAYS_LOGIN_PASSWORD:
        return None

    try:
        session = requests.Session()
        base_url = settings.ALLWAYS_BASE_URL.rstrip("/")
        login_page = session.get(f"{base_url}/login", timeout=15)
        login_page.raise_for_status()

        match = re.search(r'name="_token" value="([^"]*)"', login_page.text)
        if not match:
            print("AllWays: Could not find CSRF token on login page")
            return None
            
        token = match.group(1)
        login_response = session.post(
            f"{base_url}/login",
            data={
                "_token": token,
                "email": settings.ALLWAYS_LOGIN_EMAIL,
                "password": settings.ALLWAYS_LOGIN_PASSWORD
            },
            headers={"Referer": f"{base_url}/login"},
            allow_redirects=False,
            timeout=15
        )

        if login_response.status_code != 302:
            print(f"AllWays: Login failed with status {login_response.status_code}")
            return None

        return session
    except Exception as e:
        print(f"AllWays: Error establishing session: {e}")
        return None


def get_shipment_details(container_number: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetches container shipment voyage details using the AllWays REST API token.
    Returns (data, error_message).
    """
    base_url = settings.ALLWAYS_BASE_URL.rstrip("/")
    api_token = settings.ALLWAYS_API_TOKEN
    
    if not api_token:
        return None, "AllWays API token is not configured"

    try:
        response = requests.get(
            f"{base_url}/api/v1/container/{container_number}",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json"
            },
            timeout=20
        )
        if response.status_code == 404:
            return None, f"Container {container_number} not found in AllWays API"
            
        response.raise_for_status()
        return response.json(), None
    except Exception as e:
        return None, str(e)


def get_geo_location(container_number: str, session: Optional[requests.Session] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetches real-time GPS coordinates and location status from AllWays.
    Reuses provided session or logs in if not provided.
    Returns (data, error_message).
    """
    base_url = settings.ALLWAYS_BASE_URL.rstrip("/")
    active_session = session or create_allways_session()
    
    if not active_session:
        return None, "Could not establish authenticated AllWays web session"

    try:
        geo_response = active_session.get(
            f"{base_url}/api/open-track/container-geo-locations",
            params={"containerNumber": container_number},
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{base_url}/dashboard/shipments",
            },
            timeout=20
        )
        geo_response.raise_for_status()
        return geo_response.json(), None
    except Exception as e:
        return None, str(e)


def parse_date(date_str: Any) -> Optional[datetime]:
    if not date_str or not isinstance(date_str, str):
        return None
    try:
        return date_parser.parse(date_str)
    except Exception:
        return None


def track_container(container_number: str, session: Optional[requests.Session] = None) -> Dict[str, Any]:
    """
    Aggregates shipment details and GPS coordinates for a container.
    """
    result = {
        "container_number": container_number,
        "origin_port": None,
        "destination_port": None,
        "carrier": None,
        "vessel_and_voyage": None,
        "etd": None,
        "eta": None,
        "status": None,
        "latitude": None,
        "longitude": None,
        "location_status": None,
        "raw_response": {},
        "error_message": None
    }

    errors = []

    # 1. Fetch Shipment Details
    shipment_raw, ship_err = get_shipment_details(container_number)
    if shipment_raw:
        result["raw_response"]["shipment_details"] = shipment_raw
        result["origin_port"] = shipment_raw.get("port_of_lading")
        result["destination_port"] = shipment_raw.get("port_of_destination")
        result["carrier"] = shipment_raw.get("carrier")
        result["vessel_and_voyage"] = shipment_raw.get("vessel_and_voyage_number")
        result["etd"] = parse_date(shipment_raw.get("etd"))
        result["eta"] = parse_date(shipment_raw.get("eta"))
        result["status"] = shipment_raw.get("status")
    elif ship_err:
        errors.append(f"Shipment API: {ship_err}")

    # 2. Fetch GPS Geo-Location
    geo_raw, geo_err = get_geo_location(container_number, session=session)
    if geo_raw:
        result["raw_response"]["geo_location"] = geo_raw
        info = geo_raw.get("data", {})
        if isinstance(info, dict):
            coords = info.get("lastKnownPosition", {}).get("coordinates", [None, None])
            if isinstance(coords, list) and len(coords) >= 2:
                result["longitude"] = coords[0]
                result["latitude"] = coords[1]
            result["location_status"] = info.get("status")
    elif geo_err:
        errors.append(f"Geo Tracking: {geo_err}")

    if errors and not shipment_raw and not geo_raw:
        result["error_message"] = "; ".join(errors)

    return result


def sync_container_tracking(
    db: Session, 
    container: models.ShippingContainer, 
    session: Optional[requests.Session] = None
) -> models.ShippingContainerTracking:
    """
    Tracks an individual container and upserts record in shipping_container_tracking.
    Also updates country_of_origin on the container if origin_port is retrieved.
    """
    container_number = (container.container_name or "").strip()
    tracking_data = track_container(container_number, session=session)

    # Find existing or create new tracking record
    tracking = db.query(models.ShippingContainerTracking).filter(
        models.ShippingContainerTracking.shipping_container_id == container.id
    ).first()

    if not tracking:
        tracking = models.ShippingContainerTracking(
            shipping_container_id=container.id,
            container_number=container_number
        )
        db.add(tracking)

    # Update tracking record fields
    tracking.container_number = container_number
    if tracking_data.get("origin_port"):
        tracking.origin_port = tracking_data["origin_port"]
    if tracking_data.get("destination_port"):
        tracking.destination_port = tracking_data["destination_port"]
    if tracking_data.get("carrier"):
        tracking.carrier = tracking_data["carrier"]
    if tracking_data.get("vessel_and_voyage"):
        tracking.vessel_and_voyage = tracking_data["vessel_and_voyage"]
    if tracking_data.get("etd"):
        tracking.etd = tracking_data["etd"]
    if tracking_data.get("eta"):
        tracking.eta = tracking_data["eta"]
    if tracking_data.get("status"):
        tracking.status = tracking_data["status"]
    if tracking_data.get("latitude") is not None:
        tracking.latitude = tracking_data["latitude"]
    if tracking_data.get("longitude") is not None:
        tracking.longitude = tracking_data["longitude"]
    if tracking_data.get("location_status"):
        tracking.location_status = tracking_data["location_status"]
    if tracking_data.get("raw_response"):
        tracking.raw_response = tracking_data["raw_response"]
    
    tracking.error_message = tracking_data.get("error_message")
    tracking.last_tracked_at = datetime.utcnow()
    tracking.updated_at = datetime.utcnow()

    # Automatically update country_of_origin on container if origin_port is available
    if tracking_data.get("origin_port"):
        container.country_of_origin = tracking_data["origin_port"]

    db.commit()
    db.refresh(tracking)
    return tracking


def sync_all_containers_tracking(db: Session) -> Dict[str, Any]:
    """
    Iterates over all shipping containers in the database and updates tracking info.
    Reuses a single authenticated session for performance.
    """
    containers = db.query(models.ShippingContainer).filter(
        models.ShippingContainer.container_name.isnot(None)
    ).all()

    session = create_allways_session()
    
    success_count = 0
    failed_count = 0
    results = []

    for container in containers:
        c_num = (container.container_name or "").strip()
        if not c_num:
            continue
            
        try:
            tracking = sync_container_tracking(db, container, session=session)
            if tracking.error_message and not tracking.origin_port and not tracking.latitude:
                failed_count += 1
                status_str = f"Failed: {tracking.error_message}"
            else:
                success_count += 1
                status_str = "Success"
                
            results.append({
                "container_id": str(container.id),
                "container_number": c_num,
                "status": status_str,
                "carrier": tracking.carrier,
                "origin_port": tracking.origin_port,
                "destination_port": tracking.destination_port,
                "location_status": tracking.location_status
            })
        except Exception as exc:
            failed_count += 1
            results.append({
                "container_id": str(container.id),
                "container_number": c_num,
                "status": f"Error: {exc}"
            })

    return {
        "total_containers": len(containers),
        "synced_successfully": success_count,
        "failed": failed_count,
        "results": results
    }
