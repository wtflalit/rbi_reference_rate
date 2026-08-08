"""Google Drive upload with replace-in-place semantics.

Authentication
--------------
Primary path is a **service account**, because GitHub Actions has no browser
to complete an interactive OAuth consent screen. Point
GOOGLE_SERVICE_ACCOUNT_FILE at the key JSON and share the destination folder
with the service account's email address (Editor).

An OAuth path is kept for local use — `credentials.json` + a cached
`token.json` — so you can run against your own account without minting a
service account first. It is deliberately not used in CI.

The service-account storage-quota trap
--------------------------------------
A service account has **no Drive storage quota of its own**. Creating a file
that the service account *owns* fails with
"Service Accounts do not have storage quota". Two ways around it, both
supported here:

  * put the folder in a **Shared Drive** (set GDRIVE_SHARED_DRIVE_ID), or
  * keep the folder in your own My Drive and share it with the service
    account — files created inside a folder you own are charged to your quota.

The second is what most people want and needs no Workspace subscription.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class DriveError(RuntimeError):
    """Anything that goes wrong talking to Drive."""


class DriveClient:
    def __init__(
        self,
        folder_id: str,
        service_account_file: str | None = None,
        oauth_credentials_file: str | None = None,
        oauth_token_file: str = "token.json",
        shared_drive_id: str | None = None,
    ) -> None:
        if not folder_id:
            raise DriveError("GDRIVE_FOLDER_ID is not set")

        self.folder_id = folder_id
        self.shared_drive_id = shared_drive_id or None
        self.service = build(
            "drive",
            "v3",
            credentials=self._credentials(
                service_account_file, oauth_credentials_file, oauth_token_file
            ),
            cache_discovery=False,
        )

    # -- auth --------------------------------------------------------------

    @staticmethod
    def _credentials(
        service_account_file: str | None,
        oauth_credentials_file: str | None,
        oauth_token_file: str,
    ):
        if service_account_file and Path(service_account_file).exists():
            log.info("Authenticating with service account %s", service_account_file)
            return service_account.Credentials.from_service_account_file(
                service_account_file, scopes=SCOPES
            )

        # Local developer path.
        token_path = Path(oauth_token_file)
        creds: Credentials | None = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing cached OAuth token")
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds

        if oauth_credentials_file and Path(oauth_credentials_file).exists():
            # Imported lazily: CI never needs this and the import pulls in a
            # local web server.
            from google_auth_oauthlib.flow import InstalledAppFlow

            log.info("Starting interactive OAuth flow (a browser will open)")
            flow = InstalledAppFlow.from_client_secrets_file(
                oauth_credentials_file, SCOPES
            )
            creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds

        raise DriveError(
            "No usable Google credentials. Set GOOGLE_SERVICE_ACCOUNT_FILE to a "
            "service-account key (CI), or place credentials.json next to the "
            "project for the interactive OAuth flow (local)."
        )

    # -- helpers -----------------------------------------------------------

    def _shared_drive_kwargs(self) -> dict:
        """Extra params every call needs when the folder is in a Shared Drive."""
        params = {"supportsAllDrives": True}
        return params

    def _list_kwargs(self) -> dict:
        params = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
        if self.shared_drive_id:
            params["corpora"] = "drive"
            params["driveId"] = self.shared_drive_id
        return params

    def find_file(self, filename: str) -> dict | None:
        """Return the newest non-trashed file with this exact name, or None."""
        escaped = filename.replace("'", r"\'")
        query = (
            f"name = '{escaped}' and '{self.folder_id}' in parents "
            f"and trashed = false"
        )
        try:
            response = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id, name, modifiedTime, size, webViewLink)",
                    orderBy="modifiedTime desc",
                    pageSize=10,
                    **self._list_kwargs(),
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveError(f"Drive search for {filename!r} failed: {exc}") from exc

        files = response.get("files", [])
        if len(files) > 1:
            log.warning(
                "%d copies of %s in the folder; updating the newest and leaving "
                "the rest alone",
                len(files),
                filename,
            )
        return files[0] if files else None

    # -- operations --------------------------------------------------------

    def upload_or_replace(self, path: str | Path) -> dict:
        """Create the file, or update it in place if the name already exists.

        Updating keeps the file ID stable, so shared links and any downstream
        Sheets/Looker references keep working — that is why this does not
        simply delete-and-recreate.
        """
        path = Path(path)
        if not path.exists():
            raise DriveError(f"{path} does not exist")

        media = MediaFileUpload(str(path), mimetype=XLSX_MIME, resumable=True)
        existing = self.find_file(path.name)

        try:
            if existing:
                log.info("Replacing existing %s (id=%s)", path.name, existing["id"])
                result = (
                    self.service.files()
                    .update(
                        fileId=existing["id"],
                        media_body=media,
                        fields="id, name, webViewLink, modifiedTime",
                        **self._shared_drive_kwargs(),
                    )
                    .execute()
                )
            else:
                log.info("Uploading new %s", path.name)
                result = (
                    self.service.files()
                    .create(
                        body={"name": path.name, "parents": [self.folder_id]},
                        media_body=media,
                        fields="id, name, webViewLink, modifiedTime",
                        **self._shared_drive_kwargs(),
                    )
                    .execute()
                )
        except HttpError as exc:
            if "storageQuotaExceeded" in str(exc):
                raise DriveError(
                    "Drive rejected the upload: service accounts have no storage "
                    "quota. Either move the folder into a Shared Drive and set "
                    "GDRIVE_SHARED_DRIVE_ID, or share a folder you own with the "
                    "service account (Editor) so the file is charged to your "
                    "quota. See README > Troubleshooting."
                ) from exc
            raise DriveError(f"Upload of {path.name} failed: {exc}") from exc

        log.info("Drive: %s -> %s", result["name"], result.get("webViewLink"))
        return result

    def download_if_exists(self, filename: str, destination: str | Path) -> Path | None:
        """Fetch `filename` from the folder, or return None if it is not there."""
        existing = self.find_file(filename)
        if not existing:
            log.info("No %s in Drive yet — starting a fresh history", filename)
            return None

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        request = self.service.files().get_media(
            fileId=existing["id"], **self._shared_drive_kwargs()
        )
        try:
            with destination.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
        except HttpError as exc:
            raise DriveError(f"Download of {filename} failed: {exc}") from exc

        log.info("Downloaded existing %s from Drive", filename)
        return destination

    def check_access(self) -> str:
        """Verify credentials and folder permissions before doing real work."""
        try:
            folder = (
                self.service.files()
                .get(
                    fileId=self.folder_id,
                    fields="id, name, mimeType",
                    **self._shared_drive_kwargs(),
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveError(
                f"Cannot access folder {self.folder_id}: {exc}. Confirm the ID is "
                "right and that the folder is shared with the service account "
                "email as Editor."
            ) from exc

        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise DriveError(f"{self.folder_id} is not a folder")
        return folder.get("name", self.folder_id)


def client_from_env() -> DriveClient:
    """Build a DriveClient from environment variables."""
    return DriveClient(
        folder_id=os.environ.get("GDRIVE_FOLDER_ID", ""),
        service_account_file=os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
        ),
        oauth_credentials_file=os.environ.get(
            "GOOGLE_OAUTH_CREDENTIALS_FILE", "credentials.json"
        ),
        oauth_token_file=os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "token.json"),
        shared_drive_id=os.environ.get("GDRIVE_SHARED_DRIVE_ID"),
    )
