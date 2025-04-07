import base64
from collections import namedtuple
from dataclasses import dataclass
import os
import json
import io
from google.auth import credentials
from googleapiclient.http import (
    MediaInMemoryUpload,
    MediaIoBaseDownload,
    MediaIoBaseUpload,
)
import requests
from tempfile import mkdtemp
from typing import Dict, List, Any, NamedTuple

from openai import OpenAI
from pypdf import PdfReader

from google.auth.transport.requests import AuthorizedSession, Request
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


def img_data_from_pdf(path) -> bytes:
    reader = PdfReader(path)
    page = reader.pages[0]
    if len(page.images) == 0:
        raise ValueError(f"{path} contains no images")
    print(page.images[0].name)
    return page.images[0].data


def ai_scan(client: OpenAI, data: bytes) -> Dict[str, Any]:
    prompt = """You are an expert at extracting structured data from OCR scanning images. You will be given an image
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

    From the image you should fill in the data in the given structured format
"""
    encoded = base64.b64encode(data).decode("utf-8")

    resp = client.responses.create(
        model="gpt-4o-2024-08-06",
        input=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": str(prompt)},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                    },
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
    inbox = get_folder(svc, "Inbox")
    print(f"'{inbox['id']}' in parents and thrashed = false")
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

    print(f"Uploading into {folder} ({slips['id']})")
    for i in items:
        bd = io.BytesIO(i.img)

        filename = f"{i.scanned_results['name']}.jpg"
        meta = {
            "name": filename,
            "parents": [slips["id"]],
        }
        up_media = MediaIoBaseUpload(bd, mimetype="image/jpeg")

        try:
            file = (
                svc.files()
                .create(body=meta, media_body=up_media, fields="id")
                .execute()
            )

            print(f"Uploaded {filename} ({file.get('id')})")
        except HttpError as error:
            print(f"An error occured: {error}")
            raise error


def delete(svc, items: List[str]):
    for i in items:
        try:
            svc.files().delete(fileId=i).execute()
            print(f"Deleted {i}")
        except HttpError as error:
            raise error


def download_all(svc, creds, files: List[Dict[str, Any]]) -> List[DriveFile]:
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

            downloaded.append(DriveFile(f, file.name, b"", {}))
    return downloaded


def main():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
            with open("token.json", "w") as token:
                token.write(creds.to_json())

    try:
        service = build("drive", "v3", credentials=creds)
        inbox = get_folder(service, "Inbox")
        files = list_folder(service, inbox)
        downloaded = download_all(service, credentials, files)
        for d in downloaded:
            print(f"Downloaded: {d}")

        api_token = os.getenv("OPENAI_TOKEN")

        client = OpenAI(api_key=api_token)
        converted = []
        for d in downloaded:
            if d.path.endswith(".pdf"):
                print(f"Scanning {d.path}")
                d.img = img_data_from_pdf(d.path)
                result = ai_scan(client, d.img)
                # Next steps:
                # need to make Downloa
                d.scanned_results = result
                converted.append(d)

        upload_into(service, "Slips", converted)
        delete(service, [v.id for v in converted])

    except HttpError as error:
        print(f"An error occured: {error}")


if __name__ == "__main__":
    main()
