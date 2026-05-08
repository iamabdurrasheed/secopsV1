# SecOps Migrations Backend

A production-grade FastAPI service migrated from AWS Lambda, responsible for polling and triggering security scans across various SCM platforms.

## Setup

1. Create a virtual environment: `python -m venv .venv`
2. Activate: `.\.venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (Linux/Mac)
3. Install dependencies: `pip install -r requirements.txt`
4. Run locally: `uvicorn src.main:app`
