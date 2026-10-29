"""Worker chạy TRÊN RUNNER GitHub Actions (không chạy ở máy trạm).

  python -m goat.worker --account <owner> --runner-id <n> [--mode rescue --jobs "sub/job,sub/job"]

Bundle trên runner (do goat deploy tạo) gồm: goat/, project.yaml, manifest.csv, plan.json,
drive_folders.json, sim/<sub>/..., parsers/ (nếu có).

Mỗi job:
  1. Nếu <job>.csv đã có trên Drive -> bỏ qua (không chạy trùng).
  2. Tạo thư mục riêng _work/<sub>/<job>/, chép file input (+ run.copy_files) vào đó.
     Thay chuỗi @PRIMARIES@ trong input bằng số hạt của project (nếu có).
  3. Chạy run.command (biến: {sim_dir} {input} {job} {primaries} {threads}).
  4. Parser đọc kết quả. Lỗi => job FAILED: chỉ upload log <job>.FAILED.log, KHÔNG upload CSV.
  5. Thành công => upload 02_Flux_Data/<sub>/<SAMPLE>/<job>.csv,
     03_Simulation_Outputs/<sub>/<job>/ (file khớp run.outputs), 05_.../<sub>/<job>.log
Runner tự dừng nhận job mới khi gần hết timeout_minutes (chừa 20 phút) để kết thúc sạch.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import drive, manifest, parsers

SAFETY_MIN = 20


def _load(root: Path):
    with open(root / "project.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    rows = {r["job_id"]: r for r in manifest.read(root / "manifest.csv")}
    with open(root / "plan.json", "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(root / "drive_folders.json", "r", encoding="utf-8") as f:
        tree = json.load(f)
    return cfg, rows, plan, tree


def _write_csv(path: Path, rows: list):
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def run_job(job: dict, cfg: dict, root: Path, svc, tree: dict, account: str, runner: str, deadline: float) -> dict:
    run = cfg["run"]
    sub, name = job["subproject"], job["job"]
    flux_folder = tree["flux_samples"][sub][job["sample"]]
    rec = {"job_id": job["job_id"], "status": "", "seconds": 0.0}

    if drive.list_children(svc, flux_folder, name=f"{name}.csv"):
        rec["status"] = "SKIP_ALREADY_ON_DRIVE"
        print(f"  SKIP {job['job_id']} (đã có trên Drive)")
        return rec

    sim_dir = (root / "sim" / sub).resolve()
    work = root / "_work" / sub / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    src_input = sim_dir / job["input_file"]
    text = src_input.read_text(encoding="utf-8", errors="replace")
    (work / Path(job["input_file"]).name).write_text(text.replace("@PRIMARIES@", str(cfg["primaries"])), encoding="utf-8")
    for pattern in run.get("copy_files") or []:
        for p in sim_dir.glob(pattern):
            dst = work / p.name
            shutil.copytree(p, dst) if p.is_dir() else shutil.copy2(p, dst)

    cmd = run["command"].format(
        sim_dir=str(sim_dir), input=Path(job["input_file"]).name, job=name,
        primaries=cfg["primaries"], threads=os.cpu_count() or 2,
    )
    budget = max(60, deadline - time.time())
    print(f"  RUN  {job['job_id']} :: {cmd}")
    t0 = time.time()
    log_lines = [f"job_id: {job['job_id']}", f"account: {account} runner: {runner}", f"command: {cmd}"]
    try:
        res = subprocess.run(cmd, shell=True, cwd=str(work), capture_output=True, text=True, timeout=budget,
                             stdin=subprocess.DEVNULL)
        rc, out, err = res.returncode, res.stdout or "", res.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc, out, err = -999, str(e.stdout or ""), "TIMEOUT: vượt ngân sách thời gian runner"
    rec["seconds"] = round(time.time() - t0, 2)
    log_lines += [f"returncode: {rc}", f"seconds: {rec['seconds']}", "----- stdout (tail) -----", out[-20000:],
                  "----- stderr (tail) -----", err[-20000:]]

    rows, problem = None, None
    if rc != 0:
        problem = f"returncode {rc}"
    else:
        try:
            rows = parsers.run_parser(run["parser"], work, job, run, root)
        except Exception as e:  # noqa: BLE001
            problem = f"parser: {e}"

    log_dir = tree["sub"][sub]["05_Execution_Logs_and_Benchmarks"]
    if problem:
        rec["status"] = "FAILED"
        log_lines.insert(0, f"STATUS: FAILED ({problem})")
        lp = work / f"{name}.FAILED.log"
        lp.write_text("\n".join(log_lines), encoding="utf-8")
        drive.upload(svc, lp, log_dir)
        print(f"  FAIL {job['job_id']}: {problem}")
        shutil.rmtree(work, ignore_errors=True)
        return rec

    meta = {"job_id": job["job_id"], "subproject": sub, "job": name, "sample": job["sample"],
            "thickness_cm": job["thickness_cm"], "energy_kev": job["energy_kev"],
            "primaries": cfg["primaries"], "runtime_s": rec["seconds"], "account": account, "runner": runner}
    out_rows = [{**meta, **r} for r in rows]
    csv_path = work / f"{name}.csv"
    _write_csv(csv_path, out_rows)

    out_patterns = run.get("outputs") or []
    produced = [p for pat in out_patterns for p in work.glob(pat) if p.is_file() and p != csv_path]
    if produced:
        job_out = drive.ensure_folder(svc, tree["sub"][sub]["03_Simulation_Outputs"], name)
        for p in produced:
            drive.upload(svc, p, job_out)
    lp = work / f"{name}.log"
    lp.write_text("STATUS: OK\n" + "\n".join(log_lines), encoding="utf-8")
    drive.upload(svc, lp, log_dir)
    drive.upload(svc, csv_path, flux_folder)  # CSV upload CUỐI CÙNG: có CSV = job hoàn tất trọn vẹn
    rec["status"] = "OK"
    print(f"  OK   {job['job_id']} ({rec['seconds']} s)")
    shutil.rmtree(work, ignore_errors=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--runner-id", required=True)
    ap.add_argument("--mode", default="normal", choices=["normal", "rescue"])
    ap.add_argument("--jobs", default="", help="rescue: danh sách job_id cách nhau dấu phẩy")
    a = ap.parse_args()

    root = Path.cwd()
    cfg, rows, plan, tree = _load(root)
    t_start = time.time()
    deadline = t_start + (cfg["timeout_minutes"] - SAFETY_MIN) * 60

    if a.mode == "rescue":
        all_ids = [j.strip() for j in a.jobs.split(",") if j.strip()]
        n = int(cfg.get("runners_per_account", 10))
        my_ids = all_ids[int(a.runner_id) - 1::n]
    else:
        my_ids = plan.get(a.account, {}).get(str(a.runner_id), [])

    print("=" * 70)
    print(f"GOAT WORKER | {cfg['name']} | {a.account} #{a.runner_id} | mode={a.mode} | {len(my_ids)} job")
    print("=" * 70)
    unknown = [j for j in my_ids if j not in rows]
    if unknown:
        raise SystemExit(f"job_id không có trong manifest: {unknown[:5]}")

    svc = drive.service()
    records = []
    for i, jid in enumerate(my_ids, 1):
        if time.time() > deadline:
            print(f"  STOP: gần hết thời gian runner, còn {len(my_ids) - i + 1} job để cứu hộ.")
            records += [{"job_id": j, "status": "NOT_STARTED_TIMEOUT", "seconds": 0} for j in my_ids[i - 1:]]
            break
        print(f"[{i}/{len(my_ids)}]")
        try:
            records.append(run_job(rows[jid], cfg, root, svc, tree, a.account, a.runner_id, deadline))
        except Exception as e:  # noqa: BLE001  (lỗi hạ tầng: ghi nhận, chạy job tiếp theo)
            print(f"  ERROR {jid}: {e}")
            records.append({"job_id": jid, "status": f"ERROR: {str(e)[:200]}", "seconds": 0})

    summary = {"project": cfg["name"], "account": a.account, "runner": a.runner_id, "mode": a.mode,
               "started": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t_start)) + "Z",
               "wall_seconds": round(time.time() - t_start, 1), "jobs": records}
    sp = root / f"runner_{a.account}_{a.runner_id}_{a.mode}_{int(t_start)}.json"
    sp.write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    bench = drive.ensure_folder(svc, tree["tiers"]["05_Execution_Logs_and_Benchmarks"], "_runners")
    drive.upload(svc, sp, bench)
    okc = sum(r["status"] == "OK" for r in records)
    print(f"\nHOÀN TẤT RUNNER: OK={okc} / {len(records)}")


if __name__ == "__main__":
    main()
