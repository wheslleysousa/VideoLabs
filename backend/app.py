from __future__ import annotations

import itertools
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

APP_NAME = "VideoLabs Worker"
ROOT = Path(os.getenv("VIDEOLABS_TMP", tempfile.gettempdir())) / "videolabs"
ROOT.mkdir(parents=True, exist_ok=True)
MAX_COMBINATIONS = int(os.getenv("MAX_COMBINATIONS", "625"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "7200"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "600"))
TARGET_WIDTH = int(os.getenv("TARGET_WIDTH", "720"))
TARGET_HEIGHT = int(os.getenv("TARGET_HEIGHT", "1280"))
TARGET_FPS = int(os.getenv("TARGET_FPS", "30"))
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

app = FastAPI(title=APP_NAME, version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://videolabs.vercel.app", "http://localhost:5173"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

jobs: dict[str, dict] = {}
lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=1)


def safe_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return value.strip("-_")[:80] or "clip"


def public_job(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado ou expirado.")
        return {
            "id": job_id,
            "status": job["status"],
            "phase": job.get("phase", ""),
            "progress": job.get("progress", 0),
            "total": job.get("total", 0),
            "percent": round((job.get("progress", 0) / max(job.get("total", 1), 1)) * 100, 1),
            "error": job.get("error"),
            "created_at": job["created_at"],
            "expires_at": job["created_at"] + JOB_TTL_SECONDS,
            "download_url": f"/v1/jobs/{job_id}/download" if job["status"] == "ready" else None,
        }


def set_job(job_id: str, **updates):
    with lock:
        if job_id in jobs:
            jobs[job_id].update(updates)


def has_audio(path: Path) -> bool:
    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    return b"Audio:" in probe.stderr


def normalize(input_path: Path, output_path: Path):
    vf = (
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={TARGET_FPS}"
    )
    if has_audio(input_path):
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(output_path),
        ]
    else:
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(input_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(output_path),
        ]
    subprocess.run(cmd, check=True)


def concat(combo: tuple[Path, ...], output: Path):
    list_file = output.with_suffix(".txt")
    lines = []
    for clip in combo:
        escaped = str(clip.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", "-movflags", "+faststart", str(output),
            ],
            check=True,
        )
    finally:
        list_file.unlink(missing_ok=True)


def process_job(job_id: str):
    with lock:
        job = jobs[job_id]
        workdir = Path(job["workdir"])
        stages = job["stages"]
        mode = job["mode"]
        requested = job["requested"]

    try:
        set_job(job_id, status="processing", phase="Normalizando vídeos", progress=0)
        normalized_dir = workdir / "normalized"
        normalized_dir.mkdir(exist_ok=True)
        normalized_stages: list[list[Path]] = []

        all_inputs = sum((stage["files"] for stage in stages), [])
        normalized_count = 0
        for stage_index, stage in enumerate(stages):
            stage_files = []
            for file_index, file_path in enumerate(stage["files"]):
                src = Path(file_path)
                dst = normalized_dir / f"s{stage_index:02d}_f{file_index:03d}.mp4"
                normalize(src, dst)
                stage_files.append(dst)
                normalized_count += 1
                set_job(job_id, phase=f"Normalizando {normalized_count}/{len(all_inputs)} vídeos")
            normalized_stages.append(stage_files)

        all_combos = list(itertools.product(*normalized_stages))
        total_available = len(all_combos)
        if mode == "random":
            count = min(max(1, requested), total_available)
            rng = random.Random(job_id)
            selected = rng.sample(all_combos, count)
        else:
            selected = all_combos

        if len(selected) > MAX_COMBINATIONS:
            raise RuntimeError(f"Este worker aceita no máximo {MAX_COMBINATIONS} combinações por job.")

        set_job(job_id, total=len(selected), progress=0, phase="Gerando combinações")
        zip_path = workdir / "VideoLabs-resultados.zip"
        manifest = {
            "project": job["project_name"],
            "mode": mode,
            "total_available": total_available,
            "generated": len(selected),
            "stages": [{"name": s["name"], "files": [Path(f).name for f in s["files"]]} for s in stages],
            "outputs": [],
        }

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for idx, combo in enumerate(selected, start=1):
                output = workdir / f"VideoLabs-{idx:04d}.mp4"
                concat(combo, output)
                archive.write(output, arcname=output.name)
                manifest["outputs"].append({
                    "file": output.name,
                    "sources": [p.name for p in combo],
                })
                output.unlink(missing_ok=True)
                set_job(job_id, progress=idx, phase=f"Gerando {idx}/{len(selected)}")

            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        for src in workdir.glob("uploads/**/*"):
            if src.is_file():
                src.unlink(missing_ok=True)
        shutil.rmtree(normalized_dir, ignore_errors=True)
        set_job(job_id, status="ready", phase="Pronto para baixar", progress=len(selected), zip_path=str(zip_path))
    except Exception as exc:
        set_job(job_id, status="error", phase="Falha no processamento", error=str(exc))


def cleanup_loop():
    while True:
        now = time.time()
        expired = []
        with lock:
            for job_id, job in jobs.items():
                if now - job["created_at"] > JOB_TTL_SECONDS:
                    expired.append((job_id, job.get("workdir")))
            for job_id, _ in expired:
                jobs.pop(job_id, None)
        for _, workdir in expired:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
        time.sleep(600)


threading.Thread(target=cleanup_loop, daemon=True).start()


@app.get("/")
def root():
    return {"name": APP_NAME, "status": "ok", "version": "0.2.0"}


@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": Path(FFMPEG).name}


@app.post("/v1/jobs")
async def create_job(request: Request):
    form = await request.form()
    metadata_raw = form.get("metadata")
    if not metadata_raw:
        raise HTTPException(400, "metadata ausente")
    try:
        metadata = json.loads(str(metadata_raw))
    except json.JSONDecodeError:
        raise HTTPException(400, "metadata inválido")

    stage_meta = metadata.get("stages") or []
    if len(stage_meta) < 2:
        raise HTTPException(400, "Adicione pelo menos duas etapas.")

    job_id = uuid.uuid4().hex[:12]
    workdir = ROOT / job_id
    upload_root = workdir / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)

    stages = []
    total_bytes = 0
    try:
        for stage_index, stage in enumerate(stage_meta):
            key = stage.get("key") or f"stage_{stage_index}"
            uploads = [item for item in form.getlist(key) if isinstance(item, UploadFile)]
            if not uploads:
                raise HTTPException(400, f"A etapa '{stage.get('name', key)}' não possui vídeos.")
            stage_dir = upload_root / f"{stage_index:02d}-{safe_name(stage.get('name', key))}"
            stage_dir.mkdir(parents=True, exist_ok=True)
            stored = []
            for file_index, upload in enumerate(uploads):
                suffix = Path(upload.filename or "video.mp4").suffix.lower() or ".mp4"
                target = stage_dir / f"{file_index:03d}-{safe_name(Path(upload.filename or 'video').stem)}{suffix}"
                with target.open("wb") as out:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > MAX_UPLOAD_MB * 1024 * 1024:
                            raise HTTPException(413, f"Upload total excede {MAX_UPLOAD_MB} MB.")
                        out.write(chunk)
                stored.append(str(target))
            stages.append({"name": stage.get("name", key), "files": stored})

        total = 1
        for stage in stages:
            total *= len(stage["files"])
        if total > MAX_COMBINATIONS and metadata.get("mode", "all") == "all":
            raise HTTPException(400, f"Há {total} combinações. O limite desta versão é {MAX_COMBINATIONS} por job. Use o modo aleatório ou reduza os arquivos.")

        with lock:
            jobs[job_id] = {
                "status": "queued",
                "phase": "Na fila",
                "progress": 0,
                "total": total if metadata.get("mode", "all") == "all" else min(int(metadata.get("requested", 25)), total),
                "error": None,
                "created_at": time.time(),
                "workdir": str(workdir),
                "stages": stages,
                "mode": metadata.get("mode", "all"),
                "requested": int(metadata.get("requested", 25)),
                "project_name": safe_name(metadata.get("projectName", "Projeto VideoLabs")),
            }
        executor.submit(process_job, job_id)
        return public_job(job_id)
    except Exception:
        if job_id not in jobs:
            shutil.rmtree(workdir, ignore_errors=True)
        raise


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str):
    return public_job(job_id)


@app.get("/v1/jobs/{job_id}/download")
def download_job(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job não encontrado ou expirado.")
        if job["status"] != "ready":
            raise HTTPException(409, "O processamento ainda não terminou.")
        zip_path = Path(job["zip_path"])
        project_name = job["project_name"]
    if not zip_path.exists():
        raise HTTPException(404, "Arquivo final não está mais disponível.")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{project_name}-VideoLabs.zip")
