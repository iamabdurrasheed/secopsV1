import os
import logging
import sys
import time

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CONTAINER_NAME = os.environ.get("AZURE_CONTAINER_NAME") or os.environ.get("AZURE_STORAGE_CONTAINER", "scan-results")


def _check_credentials_configured():
    """
    Verify that Azure credentials are properly configured.
    Returns True if credentials exist, False if they should be skipped.
    """
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    
    # If connection string is empty, None, or a placeholder, credentials are not configured
    if not conn_str or conn_str.startswith("your_"):
        return False
    
    return True


def _get_client():
    """
    Create and return an Azure BlobServiceClient.
    Raises EnvironmentError if credentials are not configured.
    """
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    
    if not conn_str:
        raise EnvironmentError("AZURE_STORAGE_CONNECTION_STRING is not set")
    
    if conn_str.startswith("your_"):
        raise EnvironmentError(f"AZURE_STORAGE_CONNECTION_STRING is a placeholder: '{conn_str}'")
    
    logger.debug(f"Using connection string (first 50 chars): {conn_str[:50]}...")
    
    try:
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient.from_connection_string(conn_str)
        logger.info("Azure BlobServiceClient created successfully")
        return client
    except ImportError as e:
        raise ImportError(f"azure-storage-blob is not installed: {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to create BlobServiceClient: {e}")


def upload_file_to_blob(local_file_path: str, blob_path: str, max_retries: int = 3) -> str:
    """
    Upload file to Azure Blob Storage using exact blob path.
    
    Args:
        local_file_path: Path to file on host
        blob_path: Destination path in Azure (e.g., app/service/branch/version/file.json)
        max_retries: Number of times to retry on transient failures
    
    Returns:
        URL of uploaded blob
        
    Raises:
        EnvironmentError: If credentials are not configured
        Exception: If upload fails after retries
    """
    logger.info(f"Attempting to upload: {local_file_path} → {blob_path}")
    
    # Check if file exists
    if not os.path.isfile(local_file_path):
        raise FileNotFoundError(f"Local file does not exist: {local_file_path}")
    
    file_size = os.path.getsize(local_file_path)
    logger.info(f"File size: {file_size} bytes")
    
    # Try to get client
    try:
        client = _get_client()
    except (EnvironmentError, ImportError) as e:
        logger.warning(f"Skipping Azure upload (credentials not configured): {e}")
        raise
    
    from azure.core.exceptions import ResourceExistsError
    
    # Create container if needed
    try:
        logger.debug(f"Ensuring container exists: {CONTAINER_NAME}")
        client.create_container(CONTAINER_NAME)
        logger.info(f"Created container: {CONTAINER_NAME}")
    except ResourceExistsError:
        logger.debug(f"Container already exists: {CONTAINER_NAME}")
    except Exception as e:
        logger.error(f"Failed to create container: {e}")
        raise
    
    # Retry logic for upload
    for attempt in range(max_retries):
        try:
            logger.info(f"Uploading (attempt {attempt + 1}/{max_retries})...")
            blob_client = client.get_blob_client(container=CONTAINER_NAME, blob=blob_path)
            
            with open(local_file_path, "rb") as f:
                blob_client.upload_blob(f, overwrite=True)
            
            logger.info(f"Upload successful → {blob_client.url}")
            return blob_client.url
            
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # exponential backoff
                logger.warning(f"Upload attempt {attempt + 1} failed: {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Upload failed after {max_retries} attempts: {e}")
                raise
