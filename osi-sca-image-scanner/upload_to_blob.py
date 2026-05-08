import sys
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        logger.error("Usage: upload_to_blob.py <local_file_path> <blob_path>")
        sys.exit(1)
    
    local_file = sys.argv[1]
    blob_path = sys.argv[2]
    
    logger.info(f"Starting Azure Blob upload...")
    logger.info(f"Local file: {local_file}")
    logger.info(f"Blob path: {blob_path}")
    
    try:
        from blob_storage import upload_file_to_blob, _check_credentials_configured
        
        # Check if credentials are configured before attempting upload
        if not _check_credentials_configured():
            logger.warning("[SKIP] Azure credentials not configured in environment. Skipping upload.")
            logger.warning("[INFO] To enable Azure uploads, set AZURE_STORAGE_CONNECTION_STRING in .env")
            sys.exit(0)
        
        # Attempt upload
        url = upload_file_to_blob(local_file, blob_path)
        logger.info(f"[SUCCESS] Uploaded to: {url}")
        print(url)  # Print URL to stdout for any caller to use
        sys.exit(0)
        
    except EnvironmentError as e:
        logger.warning(f"[SKIP] Azure upload skipped — credentials not properly configured")
        logger.warning(f"[DETAILS] {e}")
        sys.exit(0)  # Non-fatal: skip upload if credentials missing
        
    except FileNotFoundError as e:
        logger.error(f"[ERROR] Local file not found: {e}")
        logger.warning("[SKIP] Upload skipped due to missing file")
        sys.exit(0)  # Non-fatal: skip if file missing
        
    except ImportError as e:
        logger.error(f"[ERROR] Azure SDK not available: {e}")
        logger.warning("[SKIP] Upload skipped — azure-storage-blob not installed")
        sys.exit(0)  # Non-fatal: skip if SDK missing
        
    except Exception as e:
        logger.error(f"[ERROR] Azure upload failed: {e}")
        logger.warning(f"[SKIP] Upload skipped due to error")
        logger.debug(f"Exception details: {type(e).__name__}: {e}")
        sys.exit(0)  # Non-fatal: skip on any error
