import logging
import os
from logging.handlers import TimedRotatingFileHandler

def setup_logger():
    # Ensure logs directory exists at the project root
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = os.path.join(log_dir, "app.log")

    # Create logger
    logger = logging.getLogger("secops_logger")
    logger.setLevel(logging.INFO)
    
    # Avoid adding handlers multiple times if logger is already configured
    if not logger.handlers:
        # Create formatter matching the requested format:
        # 2026-01-28 00:09:50 | INFO     | duplicate_service.py:108 | find_duplicates() | Found 13 duplicate matches
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(filename)s:%(lineno)d | %(funcName)s() | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # Create timed rotating file handler (rotates at midnight)
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",
            interval=1,
            backupCount=30, # Keep logs for 30 days
            encoding="utf-8"
        )
        # Sets the rotated suffix to match .YYYY-MM-DD (e.g. app.log.2026-01-30)
        file_handler.suffix = "%Y-%m-%d" 
        file_handler.setFormatter(formatter)

        # Create console handler for standard output
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        # Add handlers to the logger
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
    return logger

# Create a singleton logger instance to be imported across the app
logger = setup_logger()
