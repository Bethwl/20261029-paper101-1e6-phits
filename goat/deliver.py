"""DELIVER: đẩy kết quả cuối lên đúng 5 tầng Drive, rồi kiểm tra lại trên Drive."""

import json

from . import drive, manifest, paths, ui
from .status import _tree
from .ui import GateError


def deliver(p):
    p.require_stage("VALIDATED", "DELIVERED")
    tree = _tree(p)
    svc = drive.service()
    rows = manifest.read(p.manifest_path)

    ui.step("1/4 01_Summary_Excel_Results")
    for sub in p.subprojects:
        n = sub["name"]
        d = p.results / "01_Summary_Excel_Results" / n
        files = [f for f in sorted(d.iterdir()) if f.is_file()]
        for f in files:
            drive.upload(svc, f, tree["sub"][n]["01_Summary_Excel_Results"])
        ui.ok(f"{n}: {len(files)} file")

    ui.step("2/4 04_Input_Decks (đúng các file input đã chạy)")
    for sub in p.subprojects:
        n = sub["name"]
        target = tree["sub"][n]["04_Input_Decks"]
        files = sorted({r["input_file"] for r in rows if r["subproject"] == n})
        if sub.get("jobs_file"):
            files.append(sub["jobs_file"])
        for rel in files:
            drive.upload(svc, p.sim / n / rel, target)
        ui.ok(f"{n}: {len(files)} file input")
    drive.upload(svc, p.dir / paths.PROJECT_FILE, tree["root"])
    drive.upload(svc, p.manifest_path, tree["root"])

    ui.step("3/4 05_Execution_Logs_and_Benchmarks/_goat")
    goat_logs = drive.ensure_folder(svc, tree["tiers"]["05_Execution_Logs_and_Benchmarks"], "_goat")
    for f in sorted(p.logs.glob("*")):
        if f.is_file() and f.name != paths.DRIVE_FOLDERS_FILE:
            drive.upload(svc, f, goat_logs)
    ui.ok("đã upload state/status/checklist/rescues/deploy logs")

    ui.step("4/4 Kiểm tra lại trên Drive")
    errs = []
    for sub in p.subprojects:
        n = sub["name"]
        local = {f.name for f in (p.results / "01_Summary_Excel_Results" / n).iterdir() if f.is_file()}
        remote = {f["name"] for f in drive.list_children(svc, tree["sub"][n]["01_Summary_Excel_Results"])}
        if local - remote:
            errs.append(f"{n}: Drive thiếu {sorted(local - remote)}")
        if not drive.list_children(svc, tree["sub"][n]["02_Flux_Data"], name=f"Raw_Merged_{n}.csv"):
            errs.append(f"{n}: Drive thiếu Raw_Merged_{n}.csv")
    if errs:
        for e in errs:
            ui.fail(e)
        raise GateError("Bàn giao chưa đủ trên Drive")
    ui.ok(f"https://drive.google.com/drive/folders/{tree['root']}")
    p.set_stage("DELIVERED", json.dumps({"drive_root": tree["root"]}))
