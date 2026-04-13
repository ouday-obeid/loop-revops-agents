"""Google Drive upload utilities for the SLT revenue model workbook.

Uses a service-account credential stored in ``GDRIVE_SERVICE_ACCOUNT_JSON``
(JSON blob or path) and writes into the folder named by ``GDRIVE_FOLDER_ID``.
Callers still receive a valid response when the env is missing — the upload
is skipped, a warning is logged, and the local path is returned instead.
"""
from agents.slt_metrics.gdrive.uploader import (
    GDriveUploadResult,
    GDriveUploader,
    upload_workbook,
)

__all__ = ["GDriveUploadResult", "GDriveUploader", "upload_workbook"]
