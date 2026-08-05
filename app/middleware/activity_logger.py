from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.concurrency import run_in_threadpool
from jose import jwt
import asyncio

from app.config import settings
from app.database import SessionLocal
from app.services.activity_service import log_activity

class ActivityLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Continue with the request handling
        response = await call_next(request)
        
        # We only care about logging /api/v1 routes
        path = request.url.path
        if path.startswith("/api/v1/") and request.method != "OPTIONS":
            # Offload the logging to a thread so it doesn't block
            # the current event loop or delay the response too much.
            method = request.method
            auth_header = request.headers.get("Authorization")
            query_params = str(request.query_params)
            
            # Fire and forget logging
            asyncio.create_task(
                self.process_and_log_activity(method, path, auth_header, query_params)
            )
            
        return response

    async def process_and_log_activity(self, method: str, path: str, auth_header: str, query_params: str):
        # 1. Extract user_id from token
        user_id = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
                user_id = payload.get("sub")
            except Exception:
                pass
        
        if not user_id:
            # Skip if we cannot identify the user (unauthenticated or invalid token)
            return

        # 2. Determine Action
        action, entity_type, entity_id = self._determine_action(method, path)

        # Skip login/logout as they might already be logged manually in auth routes,
        # but the middleware ensures everything is captured. We'll leave it in.

        # 3. Log to DB using a fresh session
        def log_to_db():
            db = SessionLocal()
            try:
                log_activity(
                    db=db,
                    action=action,
                    user_id=user_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    details={"method": method, "path": path, "query": query_params}
                )
            except Exception as e:
                print(f"Failed to log user journey: {e}")
            finally:
                db.close()
        
        await run_in_threadpool(log_to_db)

    def _determine_action(self, method: str, path: str):
        """
        Map the route to a readable action and entity.
        e.g., GET /api/v1/purchase-orders -> VIEW_PURCHASE_ORDER_LIST
        """
        clean_path = path.replace("/api/v1", "")
        parts = [p for p in clean_path.split("/") if p]
        
        if not parts:
            return method, "API_CALL", None
            
        main_resource = parts[0]
        
        # Default fallbacks
        action = f"{method}_{main_resource.upper().replace('-', '_')}"
        entity_type = main_resource.upper().replace("-", "_")
        entity_id = None
        
        # Determine entity ID if present
        if len(parts) >= 2 and parts[1] not in ["summary", "filters", "comments", "sync", "export", "login", "register", "logout", "me"]:
            entity_id = parts[1]

        if main_resource == "purchase-orders":
            entity_type = "PURCHASE_ORDER"
            if "comments" in parts:
                if method == "POST": action = "ADD_PO_COMMENT"
                elif method == "PUT": action = "UPDATE_PO_COMMENT"
                elif method == "DELETE": action = "DELETE_PO_COMMENT"
                elif method == "GET": action = "VIEW_PO_COMMENTS"
            elif "sync" in parts:
                action = "SYNC_PURCHASE_ORDERS"
            elif "export" in parts:
                action = "EXPORT_PURCHASE_ORDERS"
            elif method == "GET":
                if entity_id: action = "VIEW_PURCHASE_ORDER_DETAIL"
                else: action = "VIEW_PURCHASE_ORDER_LIST"
            elif method == "PUT": action = "UPDATE_PURCHASE_ORDER"
            
        elif main_resource == "containers":
            entity_type = "CONTAINER"
            if "sync" in parts:
                action = "SYNC_CONTAINERS"
            elif method == "GET":
                if entity_id: action = "VIEW_CONTAINER_DETAIL"
                else: action = "VIEW_CONTAINER_LIST"
            elif method == "PUT": action = "UPDATE_CONTAINER"
            
        elif main_resource == "vendors":
            entity_type = "VENDOR"
            if method == "GET": action = "VIEW_VENDOR_DETAIL" if entity_id else "VIEW_VENDOR_LIST"
            
        elif main_resource == "auth":
            entity_type = "USER"
            if "login" in parts: action = "LOGIN_ATTEMPT"
            elif "logout" in parts: action = "LOGOUT"
            elif "refresh" in parts: action = "REFRESH_TOKEN"
            elif "me" in parts: action = "VIEW_PROFILE"
            
        elif main_resource == "companies":
            entity_type = "COMPANY"
            if method == "GET": action = "VIEW_COMPANY_DETAIL" if entity_id else "VIEW_COMPANY_LIST"
            
        elif main_resource == "customers":
            entity_type = "CUSTOMER"
            if method == "GET": action = "VIEW_CUSTOMER_DETAIL" if entity_id else "VIEW_CUSTOMER_LIST"
            
        elif main_resource == "warehouses":
            entity_type = "WAREHOUSE"
            if method == "GET": action = "VIEW_WAREHOUSE_DETAIL" if entity_id else "VIEW_WAREHOUSE_LIST"
            
        elif main_resource == "activities":
            entity_type = "ACTIVITY"
            if method == "GET": action = "VIEW_ACTIVITY_LOGS"
            elif method == "POST": action = "LOG_CUSTOM_ACTIVITY"

        return action[:50], entity_type[:50], entity_id
