"""HARVEST: tải toàn bộ CSV từ Drive về results/, gộp Raw_Merged theo subproject, đối chiếu 1-1 với manifest."""

import csv
import json
from pathlib import Path

from . import drive, manifest, paths, ui
from .status import _tree
from .ui import GateError

FLUX = "02_Flux_Data"
LOGS = "05_Execution_Logs_and_Benchmarks"


def _read_rows(p: Path) -> list:
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _write(p: Path, rows: list):
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def harvest(p):
    p.require_stage("COMPLETE", "HARVESTED")
    rows = manifest.read(p.manifest_path)
    tree = _tree(p)
    svc = drive.service()

    ui.step("1/3 Tải CSV từ Drive về results/02_Flux_Data/")
    got = 0
    for sub, samples in tree["flux_samples"].items():
        for sample, fid in samples.items():
            dest = p.results / FLUX / sub / sample
            for f in drive.list_children(svc, fid):
                if not f["name"].endswith(".csv"):
                    continue
                local = dest / f["name"]
                if local.exists() and str(local.stat().st_size) == str(f.get("size")):
                    continue
                drive.download(svc, f["id"], local)
                got += 1
    ui.ok(f"tải mới {got} file")

    ui.step("2/3 Đối chiếu 1-1 với manifest và gộp Raw_Merged")
    problems = []
    for sub in p.subprojects:
        n = sub["name"]
        merged = []
        for r in (x for x in rows if x["subproject"] == n):
            f = p.results / FLUX / n / r["sample"] / f"{r['job']}.csv"
            if not f.exists():
                problems.append(f"thiếu {r['job_id']}")
                continue
            data = _read_rows(f)
            if not data:
                problems.append(f"rỗng {r['job_id']}")
                continue
            if any(d.get("job_id") != r["job_id"] for d in data):
                problems.append(f"job_id trong file không khớp: {f.name}")
            merged.extend(data)
        out = p.results / FLUX / n / f"Raw_Merged_{n}.csv"
        if merged:
            _write(out, merged)
            drive.upload(svc, out, tree["sub"][n][FLUX])
            ui.ok(f"{n}: {len(merged)} dòng -> {out.relative_to(p.dir)} (đã upload Drive)")
    if problems:
        for x in problems[:20]:
            ui.fail(x)
        raise GateError(f"{len(problems)} lỗi đối chiếu; KHÔNG được phân tích khi dữ liệu chưa đủ")

    ui.step("3/3 Tải log runner & lập benchmark")
    bench_folder = drive.list_children(svc, tree["tiers"][LOGS], folders_only=True, name="_runners")
    runner_dir = p.results / LOGS / "_runners"
    if bench_folder:
        for f in drive.list_children(svc, bench_folder[0]["id"]):
            local = runner_dir / f["name"]
            if not local.exists():
                drive.download(svc, f["id"], local)
    for sub in p.subprojects:
        n = sub["name"]
        merged = _read_rows(p.results / FLUX / n / f"Raw_Merged_{n}.csv")
        bench = [{"job_id": r["job_id"], "runtime_s": r.get("runtime_s", ""), "account": r.get("account", ""),
                  "runner": r.get("runner", "")} for r in merged]
        bp = p.results / LOGS / n / f"benchmark_{n}.csv"
        bp.parent.mkdir(parents=True, exist_ok=True)
        _write(bp, bench)
        drive.upload(svc, bp, tree["sub"][n][LOGS])
        secs = [float(b["runtime_s"]) for b in bench if b["runtime_s"]]
        ui.ok(f"{n}: benchmark {len(secs)} job, tổng CPU {sum(secs) / 3600:.2f} h")
    p.set_stage("HARVESTED", f"{len(rows)} jobs merged")
