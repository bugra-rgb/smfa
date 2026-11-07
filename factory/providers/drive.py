"""Google Drive v3: durable uploads, paginated inventory and change polling."""
from __future__ import annotations

import mimetypes
from pathlib import Path

import httpx

from .base import ProviderError, bearer, checked, google_session, persist

API = "https://www.googleapis.com/drive/v3"
FILE_FIELDS = "id,name,mimeType,parents,size,modifiedTime,appProperties,webViewLink"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveProvider:
    def __init__(self, credentials: dict, client: httpx.Client | None = None):
        self.credentials = credentials
        self.client = client or httpx.Client(timeout=120, follow_redirects=False)

    @property
    def headers(self):
        return bearer(self.credentials)

    def list_files(self, folder_id: str, *, videos_only: bool = True) -> list:
        query = f"'{_q(folder_id)}' in parents and trashed = false"
        if videos_only:
            query += " and mimeType contains 'video/'"
        return self._list(query)

    def validate_folder(self, folder_id: str) -> dict:
        from .base import ProviderError
        data = checked(self.client.get(f"{API}/files/{folder_id}", headers=self.headers,
            params={"fields": "id,name,mimeType,trashed", "supportsAllDrives": "true"})).json()
        if data.get("trashed") or data.get("mimeType") != "application/vnd.google-apps.folder":
            raise ProviderError("Drive giriş adresi erişilebilir bir klasör olmalı.", "invalid_folder")
        return data

    def _list(self, query: str) -> list:
        results, token = [], None
        while True:
            params: dict[str, str | int] = {"q": query, "fields": f"nextPageToken,files({FILE_FIELDS})", "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = checked(self.client.get(f"{API}/files", params=params, headers=self.headers)).json()
            results.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                return results

    def create_folder(self, name: str, parent_id: str = "root") -> dict:
        found = self._list(f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
        if found:
            return found[0]
        return checked(self.client.post(f"{API}/files", headers=self.headers,
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]})).json()

    def ensure_folders(self, account_name: str, parent_id: str = "root") -> dict:
        root = self.create_folder("Accounts", parent_id)
        account = self.create_folder(account_name, root["id"])
        return {name.lower(): self.create_folder(name, account["id"])["id"]
                for name in ("Input", "Generated", "Published", "Failed")}

    def download(self, file_id: str, path: str) -> dict:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            with self.client.stream("GET", f"{API}/files/{file_id}", headers=self.headers,
                                    params={"alt": "media", "supportsAllDrives": "true"}) as response:
                checked(response)
                with temporary.open("wb") as out:
                    for chunk in response.iter_bytes(1024 * 1024):
                        out.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(target), "file_id": file_id, "size": target.stat().st_size}

    def upload(self, path: str, folder_id: str, idempotency_key: str,
               checkpoint=None, save_checkpoint=None) -> dict:
        cp = dict(checkpoint or {})
        file_path = Path(path)
        size = file_path.stat().st_size
        if cp.get("file_id"):
            existing = self.client.get(f"{API}/files/{cp['file_id']}", headers=self.headers,
                                       params={"fields": FILE_FIELDS, "supportsAllDrives": "true"})
            if existing.status_code == 200 and int(existing.json().get("size", 0)) == size:
                return {**existing.json(), "file_id": cp["file_id"], "status": "COMPLETED", "checkpoint": cp}
            if existing.status_code not in (200, 404):
                checked(existing)
        else:
            # A pre-generated Drive ID is an actual server-side duplicate guard.
            ids = checked(self.client.get(f"{API}/files/generateIds", headers=self.headers, params={"count": 1, "space": "drive", "type": "files"})).json()
            persist(cp, save_checkpoint, file_id=ids["ids"][0])
        offset = 0
        try:
            if cp.get("session_url"):
                url = google_session(cp["session_url"])
                response = self.client.put(url, headers={**self.headers, "Content-Length": "0", "Content-Range": f"bytes */{size}"}, content=b"")
                if response.status_code in (200, 201):
                    persist(cp, save_checkpoint, complete=True)
                    return {**response.json(), "file_id": cp["file_id"], "status": "COMPLETED", "checkpoint": cp}
                if response.status_code in (404, 410):
                    # Same pre-generated ID is reused, so expiration cannot create a second file.
                    persist(cp, save_checkpoint, session_url=None)
                elif response.status_code == 308:
                    offset = int(response.headers.get("Range", "bytes=0--1").rsplit("-", 1)[-1]) + 1 if response.headers.get("Range") else 0
                else:
                    checked(response)
            if not cp.get("session_url"):
                response = checked(self.client.post("https://www.googleapis.com/upload/drive/v3/files",
                    headers={**self.headers, "X-Upload-Content-Type": mimetypes.guess_type(path)[0] or "video/mp4", "X-Upload-Content-Length": str(size)},
                    params={"uploadType": "resumable", "fields": FILE_FIELDS, "supportsAllDrives": "true"},
                    json={"id": cp["file_id"], "name": file_path.name, "parents": [folder_id], "appProperties": {"idempotency_key": idempotency_key}}))
                persist(cp, save_checkpoint, session_url=google_session(response.headers["Location"]))
            # Chunk sizes are multiples of 256 KiB; memory stays bounded.
            with file_path.open("rb") as stream:
                stream.seek(offset)
                while offset < size:
                    chunk = stream.read(8 * 1024 * 1024)
                    response = self.client.put(cp["session_url"], headers={**self.headers, "Content-Type": "video/mp4", "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}"}, content=chunk)
                    if response.status_code in (200, 201):
                        persist(cp, save_checkpoint, complete=True)
                        return {**response.json(), "file_id": cp["file_id"], "status": "COMPLETED", "checkpoint": cp}
                    if response.status_code != 308:
                        checked(response)
                        raise ProviderError("Drive yüklemesi beklenmeyen yanıt verdi.", "drive_upload_failed", checkpoint=cp)
                    next_offset = int(response.headers["Range"].rsplit("-", 1)[-1]) + 1
                    if next_offset <= offset:
                        raise ProviderError("Drive yüklemesi ilerlemiyor.", "drive_upload_stalled", True, checkpoint=cp)
                    offset = next_offset
                    stream.seek(offset)
            raise ProviderError("Drive yüklemesi tamamlanmadı.", "drive_upload_incomplete", True, checkpoint=cp)
        except httpx.TransportError as exc:
            raise ProviderError("Drive bağlantısı kesildi; kayıtlı oturumdan devam edilecek.", "provider_timeout", True, checkpoint=cp) from exc

    def move(self, file_id: str, folder_id: str) -> dict:
        current = checked(self.client.get(f"{API}/files/{file_id}", headers=self.headers, params={"fields": "parents", "supportsAllDrives": "true"})).json()
        parents = current.get("parents", [])
        if parents == [folder_id]:
            return {"id": file_id, "parents": parents}
        params = {"addParents": folder_id, "fields": FILE_FIELDS, "supportsAllDrives": "true"}
        old = [p for p in parents if p != folder_id]
        if old:
            params["removeParents"] = ",".join(old)
        return checked(self.client.patch(f"{API}/files/{file_id}", headers=self.headers, params=params, json={})).json()

    def changes(self, cursor: str | None = None) -> dict:
        if not cursor:
            data = checked(self.client.get(f"{API}/changes/startPageToken", headers=self.headers)).json()
            return {"changes": [], "cursor": data["startPageToken"]}
        results = []
        while True:
            data = checked(self.client.get(f"{API}/changes", headers=self.headers,
                params={"pageToken": cursor, "pageSize": 1000, "spaces": "drive", "includeRemoved": "true", "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))"})).json()
            results.extend(data.get("changes", []))
            if data.get("nextPageToken"):
                cursor = data["nextPageToken"]
            else:
                return {"changes": results, "cursor": data.get("newStartPageToken", cursor)}
