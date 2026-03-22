import base64
from dataclasses import dataclass
import os
import json
import io
import sys
import mimetypes
from pathlib import Path
from googleapiclient.http import (
    MediaIoBaseDownload,
    MediaIoBaseUpload,
)
from tempfile import mkdtemp
from typing import Dict, List, Any

from openai import OpenAI
from pypdf import PdfReader

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]


@dataclass
class DriveFile(object):
    drive: Dict[str, Any]
    path: str
    img: bytes
    mimetype: str | None
    scanned_results: Dict[str, Any]

    @property
    def id(self):
        return self.drive["id"]

    @property
    def name(self):
        return self.drive["name"]

    @property
    def new_name(self):
        return self.scanned_results.get("name")


def log_section(title: str) -> None:
    print(f"\n== {title} ==")


def log_event(stage: str, message: str) -> None:
    print(f"[{stage}] {message}")


def img_data_from_pdf(path: Path) -> bytes:
    reader = PdfReader(path)
    page = reader.pages[0]
    if len(page.images) == 0:
        raise ValueError(f"{path} contains no images")
    log_event("pdf", f"{path.name}: extracted {page.images[0].name}")
    return page.images[0].data


def load_img(path: Path) -> bytes:
    with open(path, "rb") as fd:
        return fd.read()


def upload_payload(item: DriveFile) -> tuple[bytes, str]:
    path = Path(item.path)
    if path.suffix.lower() == ".pdf":
        return load_img(path), "application/pdf"

    if item.mimetype is not None:
        return item.img, item.mimetype

    guessed, _ = mimetypes.guess_type(path)
    return item.img, guessed or "application/octet-stream"


def ai_scan(
    client: OpenAI, mimetype: str | None, data: bytes, filename: str | None = None
) -> Dict[str, Any]:
    prompt = """You are an expert at extracting structured data from OCR scanning images and PDFs. You will be given a receipt image or PDF
    from which you should create name that follows the following format: `{merchantnameformat:lowercase-hypen-as-spaces}_{dateformat:2005-04-03}_{123.50ZAR}`.

    Here are a few examples for the name format
    <example>
    pick-n-pay_2025-03-31_1336.37zar
    </example>
    <example>
    checkers_2025-01-31_10.00zar
    </example>
    <example>
    hussar-gril_2025-02-28_998.09zar
    </example>

    If the currency is not ZAR, use the following conversion:
    1 USD / 18.50 ZAR
    1 GBP / 22.50 ZAR

    IMPORTANT: For statements that contain amounts for multiple transactions, only consider transactions for the closest to date month. Disregard mentions of previous balances.

    From the image you should fill in the data in the given structured format
"""
    encoded = base64.b64encode(data).decode("utf-8")
    input_content: Dict[str, str]
    if mimetype == "application/pdf":
        input_content = {
            "type": "input_file",
            "filename": filename or "document.pdf",
            "file_data": f"data:application/pdf;base64,{encoded}",
        }
    else:
        input_content = {
            "type": "input_image",
            "image_url": f"data:{mimetype};base64,{encoded}",
        }

    resp = client.responses.create(
        model="gpt-4o-2024-08-06",
        input=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": str(prompt)},
                    input_content,
                ],
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "receipt_data",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "merchant": {"type": "string"},
                        "total": {"type": "number"},
                        "date": {"type": "string"},
                    },
                    "required": ["name", "merchant", "total", "date"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        },
    )

    raw = resp.output_text
    return json.loads(raw)


def get_folder(svc, name) -> Dict[str, int]:
    results = (
        svc.files()
        .list(
            pageSize=10,
            q=f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="nextPageToken, files(id, name)",
        )
        .execute()
    )
    items = results.get("files", [])
    return items[0]


def list_folder(svc, folder: Dict[str, Any]) -> List[Dict[str, Any]]:
    log_event("drive", f"Listing {folder['name']} ({folder['id']})")
    results = (
        svc.files()
        .list(
            pageSize=10,
            q=f"'{folder['id']}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name)",
        )
        .execute()
    )

    return results.get("files", [])


def upload_into(svc, folder: str, items: List[DriveFile]):
    slips = get_folder(svc, folder)

    if len(items) == 0:
        log_event("upload", "No items to upload")
        return

    log_section("Upload")
    log_event("upload", f"Destination: {folder} ({slips['id']})")
    for i in items:
        upload_bytes, upload_mimetype = upload_payload(i)
        bd = io.BytesIO(upload_bytes)

        ext = mimetypes.guess_extension(upload_mimetype) or Path(i.path).suffix or ""

        filename = f"{i.scanned_results['name']}{ext}"
        log_event("upload", f"{Path(i.path).name} -> {filename}")
        meta = {
            "name": filename,
            "parents": [slips["id"]],
        }

        up_media = MediaIoBaseUpload(bd, mimetype=upload_mimetype)

        try:
            file = (
                svc.files()
                .create(body=meta, media_body=up_media, fields="id")
                .execute()
            )

            log_event("upload", f"Created {filename} ({file.get('id')})")
        except HttpError as error:
            log_event("error", f"Upload failed for {filename}: {error}")
            raise error


def delete(svc, items: List[str]):
    log_section("Cleanup")
    for i in items:
        try:
            svc.files().delete(fileId=i).execute()
            log_event("delete", f"Removed source file {i}")
        except HttpError as error:
            raise error


def download_all(svc, files: List[Dict[str, Any]]) -> List[DriveFile]:
    dst = mkdtemp(suffix="drive-downloads")
    downloaded = []
    for f in files:
        request = svc.files().get_media(fileId=f["id"])
        path = os.path.join(dst, f["name"])
        with open(path, "wb") as file:
            downloader = MediaIoBaseDownload(file, request)
            done = False

            while done is False:
                _, done = downloader.next_chunk()

            downloaded.append(
                DriveFile(f, file.name, b"", "application/octet-stream", {})
            )
    return downloaded


def save_credentials(creds: Credentials) -> None:
    with open("token.json", "w") as token:
        token.write(creds.to_json())


def authorize_user() -> Credentials:
    credentials_path = Path("credentials.json")
    if not credentials_path.exists():
        raise FileNotFoundError("Missing credentials.json")

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds


def load_credentials() -> Credentials:
    creds = None
    token_path = Path("token.json")

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, json.JSONDecodeError):
            creds = None

    if not creds:
        return authorize_user()

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
            return creds
        except RefreshError:
            return authorize_user()

    return authorize_user()


def main():
    creds = load_credentials()

    try:
        log_section("Drive")
        service = build("drive", "v3", credentials=creds)
        inbox = get_folder(service, "Inbox")
        files = list_folder(service, inbox)

        if len(files) == 0:
            log_event("done", f"No files in {inbox['name']}")
            sys.exit(0)

        log_section("Download")
        downloaded = download_all(service, files)
        for d in downloaded:
            log_event("download", f"{d.name} -> {d.path}")

        api_token = os.getenv("OPENAI_TOKEN")
        if not api_token:
            raise ValueError("Missing OPENAI_TOKEN")

        client = OpenAI(api_key=api_token)
        converted = []
        log_section("Scan")
        for d in downloaded:
            path = Path(d.path)
            if path.suffix.lower() == ".pdf":
                try:
                    d.img = img_data_from_pdf(path)
                    d.mimetype = "image/jpeg"
                except Exception as error:
                    log_event("pdf", f"{path.name}: extraction failed, using PDF input ({error})")
                    d.img = load_img(path)
                    d.mimetype = "application/pdf"
            elif path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                d.mimetype, _ = mimetypes.guess_type(path)
                d.img = load_img(path)
            log_event("scan", f"{path.name} ({d.mimetype})")
            result = ai_scan(client, d.mimetype, d.img, filename=path.name)
            d.scanned_results = result
            log_event("scan", f"{path.name} -> {result['name']}")
            converted.append(d)

        upload_into(service, "Slips", converted)
        delete(service, [v.id for v in converted])

    except HttpError as error:
        log_event("error", f"Drive request failed: {error}")


if __name__ == "__main__":
    main()
