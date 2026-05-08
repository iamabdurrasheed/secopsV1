from fastapi import FastAPI, status
from src.utils.logger import logger

app = FastAPI()

logger.info("FastAPI application is starting up...")

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    logger.info("Health check endpoint was called")
    return {"status": "SecOps polling server is running"}