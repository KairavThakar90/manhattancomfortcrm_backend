import os
import uuid
from google.cloud import storage
from fastapi import UploadFile
from app.config import settings

def get_gcs_client():
    if not settings.GOOGLE_APPLICATION_CREDENTIALS or not settings.GCS_BUCKET_NAME:
        return None
    try:
        return storage.Client.from_service_account_json(settings.GOOGLE_APPLICATION_CREDENTIALS)
    except Exception as e:
        print(f"Error initializing GCS client: {e}")
        return None

async def upload_file_to_gcs(file_obj: bytes, original_filename: str, content_type: str) -> str:
    """
    Uploads a file to Google Cloud Storage and returns the public URL.
    Generates a unique filename to prevent collisions.
    """
    client = get_gcs_client()
    if not client:
        raise Exception("Google Cloud Storage is not configured properly.")

    bucket = client.bucket(settings.GCS_BUCKET_NAME)
    
    # Generate unique filename
    ext = original_filename.split(".")[-1] if "." in original_filename else ""
    unique_filename = f"attachments/{uuid.uuid4().hex}_{original_filename}"
    
    from starlette.concurrency import run_in_threadpool
    
    blob = bucket.blob(unique_filename)
    
    # Upload the file bytes in a separate thread so it doesn't block the async event loop
    await run_in_threadpool(blob.upload_from_string, file_obj, content_type=content_type)
    
    # Note: Uniform bucket-level access is enabled on this bucket.
    # The bucket itself has Storage Object Viewer permission for allUsers, 
    # so we don't need to (and cannot) set individual objects to public.
    # Just return the public URL directly!

    return blob.public_url
