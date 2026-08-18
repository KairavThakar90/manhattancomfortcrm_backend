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
 
def generate_gcs_filename(original_filename: str):
    """Generates the unique filename and public URL instantly without network calls"""
    unique_filename = f"attachments/{uuid.uuid4().hex}_{original_filename}"
    public_url = f"https://storage.googleapis.com/{settings.GCS_BUCKET_NAME}/{unique_filename}"
    return unique_filename, public_url
 
async def upload_file_to_gcs(file_obj: bytes, original_filename: str, content_type: str) -> str:
    """
    Legacy wrapper for synchronous foreground uploads used by other modules.
    """
    unique_filename, public_url = generate_gcs_filename(original_filename)
    await upload_file_to_gcs_background(file_obj, unique_filename, content_type)
    return public_url
 
async def upload_file_to_gcs_background(file_obj: bytes, unique_filename: str, content_type: str):
    """
    Background task to upload a file to Google Cloud Storage.
    """
    client = get_gcs_client()
    if not client:
        return
 
    bucket = client.bucket(settings.GCS_BUCKET_NAME)
   
    from starlette.concurrency import run_in_threadpool
    import io
    def compress_and_upload(file_data: bytes, c_type: str, dest_blob):
        # Compress image if applicable and Pillow is installed
        if c_type in ["image/jpeg", "image/jpg", "image/png"]:
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(file_data))
                if img.mode in ("RGBA", "P") and c_type in ["image/jpeg", "image/jpg"]:
                    img = img.convert("RGB")
                   
                # Resize if it's very large (max 1920x1920)
                img.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
               
                output = io.BytesIO()
                img_format = "JPEG" if c_type in ["image/jpeg", "image/jpg"] else "PNG"
               
                if img_format == "JPEG":
                    img.save(output, format=img_format, quality=75, optimize=True)
                else:
                    img.save(output, format=img_format, optimize=True)
                   
                compressed_bytes = output.getvalue()
                dest_blob.upload_from_string(compressed_bytes, content_type=c_type)
                return
            except ImportError:
                print("Pillow not installed. Skipping image compression.")
            except Exception as e:
                print(f"Failed to compress image: {e}")
               
        # Fallback to uploading original raw data
        dest_blob.upload_from_string(file_data, content_type=c_type)
 
    blob = bucket.blob(unique_filename)
   
    # Run both the CPU-intensive compression and the blocking upload in a separate thread
    await run_in_threadpool(compress_and_upload, file_obj, content_type, blob)