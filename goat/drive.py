"""Google Drive: một module duy nhất cho cả máy trạm và runner.

Cấu trúc bắt buộc trên Drive (giống hệt results/ ở máy trạm):
  <parent_folder_id | My Drive/GOAT_Projects>/<TÊN PROJECT>/
     01_Summary_Excel_Results/<sub>/*.xlsx
     02_Flux_Data/<sub>/<SAMPLE>/<job>.csv          (1 job = 1 CSV)
     02_Flux_Data/<sub>/Raw_Merged_<sub>.csv
     03_Simulation_Outputs/<sub>/<job>/...           (KHÔNG zip)
     04_Input_Decks/<sub>/...                        (file input gốc)
     05_Execution_Logs_and_Benchmarks/<sub>/<job>.log, benchmark, status
"""

import io
import json
import random
import time
from pathlib import Path

from . import paths

FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_PARENT_NAME = "GOAT_Projects"  # thư mục chung trong My Drive khi project không chỉ định parent_folder_id
_RETRY_MARKERS = ("ratelimitexceeded", "userratelimit", "403", "429", "500", "502", "503", "quota", "timed out", "connection")


def retry(fn, tries: int = 7, base: float = 2.0):
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if i == tries or not any(m in msg for m in _RETRY_MARKERS):
                raise
            time.sleep(base * (2 ** (i - 1)) + random.uniform(0.5, 3.0))


def service(oauth_str: str = None):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from . import creds

    info = json.loads(oauth_str or creds.gdrive_oauth_str())
    c = Credentials.from_authorized_user_info(info)
    return build("drive", "v3", credentials=c, cache_discovery=False)


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def list_children(svc, parent_id: str, folders_only: bool = False, name: str = None) -> list:
    q = f"'{parent_id}' in parents and trashed = false"
    if folders_only:
        q += f" and mimeType = '{FOLDER_MIME}'"
    if name:
        q += f" and name = '{_q(name)}'"
    out, token = [], None
    while True:
        res = retry(lambda: svc.files().list(
            q=q, pageSize=1000, pageToken=token, fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute())
        out.extend(res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            return out


def ensure_folder(svc, parent_id: str, name: str) -> str:
    found = list_children(svc, parent_id, folders_only=True, name=name)
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    return retry(lambda: svc.files().create(body=meta, fields="id", supportsAllDrives=True).execute())["id"]


def upload(svc, local: Path, folder_id: str, name: str = None) -> str:
    """Tạo mới hoặc ghi đè file cùng tên trong folder. Trả về file id."""
    from googleapiclient.http import MediaFileUpload

    local = Path(local)
    name = name or local.name
    existing = list_children(svc, folder_id, name=name)
    media = MediaFileUpload(str(local), resumable=True)
    if existing:
        fid = existing[0]["id"]
        retry(lambda: svc.files().update(fileId=fid, media_body=media, supportsAllDrives=True).execute())
        return fid
    meta = {"name": name, "parents": [folder_id]}
    return retry(lambda: svc.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute())["id"]


def upload_dir(svc, local_dir: Path, folder_id: str):
    for p in sorted(Path(local_dir).iterdir()):
        if p.is_dir():
            upload_dir(svc, p, ensure_folder(svc, folder_id, p.name))
        else:
            upload(svc, p, folder_id)


def download(svc, file_id: str, dest: Path):
    from googleapiclient.http import MediaIoBaseDownload

    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = retry(dl.next_chunk)
    dest.write_bytes(buf.getvalue())


def init_project_tree(svc, project) -> dict:
    """Tạo (hoặc tìm lại) toàn bộ cây thư mục của project trên Drive. Idempotent."""
    parent = (project.cfg.get("drive") or {}).get("parent_folder_id") or ensure_folder(svc, "root", DEFAULT_PARENT_NAME)
    root = project.cfg["drive"].get("root_folder_id") or ensure_folder(svc, parent, project.name)
    tree = {"project": project.name, "root": root, "tiers": {}, "sub": {}, "flux_samples": {}}
    for t in paths.TIERS:
        tree["tiers"][t] = ensure_folder(svc, root, t)
    for sub in project.subprojects:
        n = sub["name"]
        tree["sub"][n] = {t: ensure_folder(svc, tree["tiers"][t], n) for t in paths.TIERS}
        samples = {s.upper() for s in sub.get("samples", [])}
        if sub.get("blank"):
            samples.add(sub["blank"]["sample"].upper())
        if sub.get("jobs_file"):
            import csv
            with open(project.sim / n / sub["jobs_file"], "r", encoding="utf-8") as f:
                samples |= {r["sample"].upper() for r in csv.DictReader(f)}
        tree["flux_samples"][n] = {s: ensure_folder(svc, tree["sub"][n]["02_Flux_Data"], s) for s in sorted(samples)}
    return tree
