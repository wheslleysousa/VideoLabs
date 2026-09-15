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
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

APP_NAME = "VideoLabs Worker"
ROOT = Path(os.getenv("VIDEOLABS_TMP", tempfile.gettempdir())) / "videolabs"
ROOT.mkdir(parents=True, exist_ok=True)
MAX_COMBINATIONS = int(os.getenv("MAX_COMBINATIONS", "625"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "7200"))
TARGET_FPS = 30
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
COPY_CODECS = ("h264", "avc1", "hevc", "h265")
PROFILES = {
    "camera4k": {"width": 2160, "height": 3840, "crf": "18", "audio": "192k", "label": "4K Câmera", "threads": "1"},
    "high": {"width": 1080, "height": 1920, "crf": "21", "audio": "128k", "label": "1080p Alta", "threads": "2"},
    "fast": {"width": 720, "height": 1280, "crf": "23", "audio": "112k", "label": "720p Rápida", "threads": "2"},
}
SILENCE_THRESHOLDS = (0.5, 1.0, 1.5, 2.0)
SILENCE_PADDINGS = (0.10, 0.15, 0.25)

app = FastAPI(title=APP_NAME, version="0.8.0")
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


def safe_name(value: object) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value).strip()).strip("-_")
    return text[:80] or "clip"


def file_part(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text[:64] or "Etapa"


def state_path(job_id: str) -> Path:
    return ROOT / job_id / "job.json"


def persist(job_id: str) -> None:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        data = dict(job)
    try:
        state_path(job_id).parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path(job_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path(job_id))
    except Exception:
        pass


def set_job(job_id: str, **updates: object) -> None:
    with lock:
        if job_id in jobs:
            jobs[job_id].update(updates)
    persist(job_id)


def restore_job(job_id: str) -> dict | None:
    path = state_path(job_id)
    if not path.exists():
        return None
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - job.get("created_at", 0) > JOB_TTL_SECONDS:
            return None
        with lock:
            jobs[job_id] = job
        return job
    except Exception:
        return None


def get_job_or_404(job_id: str) -> dict:
    with lock:
        job = jobs.get(job_id)
    if not job:
        job = restore_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado ou expirado.")
    return job


def public_job(job_id: str) -> dict:
    job = get_job_or_404(job_id)
    outputs = job.get("outputs", []) if job.get("status") == "ready" else []
    return {
        "id": job_id,
        "status": job["status"],
        "phase": job.get("phase", ""),
        "progress": job.get("progress", 0),
        "total": job.get("total", 0),
        "percent": round(job.get("overall_percent", 0), 1),
        "error": job.get("error"),
        "quality": job.get("quality", "camera4k"),
        "remove_silence": job.get("remove_silence", False),
        "silence_threshold": job.get("silence_threshold", 1.0),
        "created_at": job["created_at"],
        "expires_at": job["created_at"] + JOB_TTL_SECONDS,
        "download_url": f"/v1/jobs/{job_id}/download" if job.get("status") == "ready" else None,
        "outputs": outputs,
    }


def run(args: list[str]) -> None:
    full = [FFMPEG, "-hide_banner", "-loglevel", "error", "-filter_threads", "1"] + args
    result = subprocess.run(full, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        msg = result.stderr.decode("utf-8", "ignore").strip()
        raise RuntimeError(msg[-1600:] or f"FFmpeg saiu com código {result.returncode}.")


def ffmpeg_stderr(args: list[str]) -> str:
    return subprocess.run(
        [FFMPEG, "-hide_banner"] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    ).stderr


def probe(path: Path) -> tuple[str, int, int, float, str, int, bool]:
    stderr = ffmpeg_stderr(["-threads", "1", "-i", str(path)])
    video = re.search(r"Video:\s*([a-zA-Z0-9_]+).*?,\s*(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) fps", stderr)
    audio = re.search(r"Audio:\s*([a-zA-Z0-9_]+).*?,\s*(\d+) Hz", stderr)
    return (
        video.group(1).lower() if video else "",
        int(video.group(2)) if video else 0,
        int(video.group(3)) if video else 0,
        round(float(video.group(4)), 2) if video else 0,
        audio.group(1).lower() if audio else "",
        int(audio.group(2)) if audio else 0,
        bool(audio),
    )


def media_duration(path: Path) -> float:
    stderr = ffmpeg_stderr(["-threads", "1", "-i", str(path)])
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return round(hours * 3600 + minutes * 60 + seconds, 2)


def has_audio(path: Path) -> bool:
    return b"Audio:" in subprocess.run(
        [FFMPEG, "-hide_banner", "-threads", "1", "-i", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ).stderr


def exact_compatible(probes: list[tuple]) -> bool:
    if not probes or any(not item[0] for item in probes):
        return False
    first = probes[0]
    return all(item == first for item in probes) and first[0] in COPY_CODECS and first[6]


def video_compatible(probes: list[tuple]) -> bool:
    if not probes or any(not item[0] for item in probes):
        return False
    first = probes[0]
    base = first[:4]
    return first[0] in COPY_CODECS and all(item[:4] == base for item in probes)


def target_dimensions(quality: str, reference: tuple | None) -> tuple[int, int]:
    profile = PROFILES.get(quality, PROFILES["camera4k"])
    width, height = profile["width"], profile["height"]
    if reference and reference[1] > reference[2]:
        return height, width
    return width, height


def video_encoder_args(quality: str) -> list[str]:
    profile = PROFILES.get(quality, PROFILES["camera4k"])
    args = [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", profile["crf"],
        "-pix_fmt", "yuv420p",
        "-threads", profile.get("threads", "1"),
    ]
    if quality == "camera4k":
        args += ["-tune", "zerolatency"]
    return args


def audio_encoder_args(quality: str) -> list[str]:
    profile = PROFILES.get(quality, PROFILES["camera4k"])
    return ["-c:a", "aac", "-b:a", profile["audio"], "-ar", "48000", "-ac", "2"]


def normalize(src: Path, dst: Path, quality: str, reference: tuple | None = None) -> None:
    width, height = target_dimensions(quality, reference)
    scaler = "lanczos" if quality in ("camera4k", "high") else "bilinear"
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={scaler},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={TARGET_FPS}"
    args = ["-y", "-i", str(src)]
    if has_audio(src):
        args += ["-vf", vf] + video_encoder_args(quality) + audio_encoder_args(quality) + ["-movflags", "+faststart", str(dst)]
    else:
        args += [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-vf", vf,
        ] + video_encoder_args(quality) + audio_encoder_args(quality) + ["-movflags", "+faststart", str(dst)]
    run(args)


def normalize_audio_only(src: Path, dst: Path, quality: str) -> None:
    audio_args = audio_encoder_args(quality)
    if has_audio(src):
        run(["-y", "-i", str(src), "-c:v", "copy"] + audio_args + ["-movflags", "+faststart", str(dst)])
    else:
        run([
            "-y", "-i", str(src), "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "copy",
        ] + audio_args + ["-movflags", "+faststart", str(dst)])


def detect_silence(src: Path, threshold: float, padding: float) -> list[tuple[float, float]]:
    if not has_audio(src):
        return []
    stderr = ffmpeg_stderr([
        "-threads", "1", "-i", str(src),
        "-af", f"silencedetect=noise=-35dB:d={threshold}",
        "-f", "null", "-",
    ])
    starts: list[float] = []
    intervals: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([\d.]+)", line)
        if start_match:
            starts.append(float(start_match.group(1)))
            continue
        end_match = re.search(r"silence_end:\s*([\d.]+)", line)
        if end_match and starts:
            start = starts.pop(0)
            end = float(end_match.group(1))
            cut_start = start + padding
            cut_end = end - padding
            if cut_end - cut_start >= 0.04:
                intervals.append((max(0.0, cut_start), max(0.0, cut_end)))
    if starts:
        duration = media_duration(src)
        for start in starts:
            cut_start = start + padding
            cut_end = duration - padding
            if cut_end - cut_start >= 0.04:
                intervals.append((max(0.0, cut_start), max(0.0, cut_end)))
    return intervals


def silence_filter(intervals: list[tuple[float, float]]) -> str:
    pieces = [f"between(t,{start:.3f},{end:.3f})" for start, end in intervals]
    return "not(" + "+".join(pieces) + ")"


def remove_silence(
    src: Path,
    dst: Path,
    quality: str,
    reference: tuple | None,
    source_probe: tuple | None,
    threshold: float,
    padding: float,
    preserve_camera_geometry: bool,
) -> int:
    intervals = detect_silence(src, threshold, padding)
    has_src_audio = has_audio(src)
    args = ["-y", "-i", str(src)]
    filter_expr = silence_filter(intervals) if intervals else ""
    vf_parts: list[str] = []

    fps = source_probe[3] if source_probe and source_probe[3] else TARGET_FPS
    if intervals:
        vf_parts += [f"select='{filter_expr}'", f"setpts=N/({fps}*TB)"]

    if quality == "camera4k" and preserve_camera_geometry:
        vf_parts += [f"fps={fps}", "setsar=1"]
    else:
        width, height = target_dimensions(quality, reference)
        scaler = "lanczos" if quality in ("camera4k", "high") else "bilinear"
        vf_parts += [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={scaler}",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
            f"fps={TARGET_FPS}",
        ]

    if has_src_audio:
        if vf_parts:
            args += ["-vf", ",".join(vf_parts)]
        if intervals:
            args += ["-af", f"aselect='{filter_expr}',asetpts=N/SR/TB"]
        args += video_encoder_args(quality) + audio_encoder_args(quality) + ["-movflags", "+faststart", str(dst)]
    else:
        args += [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        ]
        if vf_parts:
            args += ["-vf", ",".join(vf_parts)]
        args += video_encoder_args(quality) + audio_encoder_args(quality) + ["-movflags", "+faststart", str(dst)]
    run(args)
    return len(intervals)


def concat(combo: tuple[dict, ...], out: Path) -> None:
    list_file = out.with_suffix(".txt")
    list_file.write_text(
        "\n".join("file '" + str(item["path"].resolve()).replace("'", "'\\''") + "'" for item in combo) + "\n",
        encoding="utf-8",
    )
    try:
        run(["-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(out)])
    finally:
        list_file.unlink(missing_ok=True)


def download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "VideoLabs/0.8"})
    total = 0
    with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as file:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 105 * 1024 * 1024:
                raise RuntimeError("Um vídeo remoto excede o limite atual de 100 MB.")
            file.write(chunk)


def output_name(combo: tuple[dict, ...], seen: dict[str, int]) -> str:
    base = " - ".join(file_part(item.get("label") or Path(item.get("original_name", "video")).stem) for item in combo)
    base = base[:190].strip(" .-") or "VideoLabs"
    number = seen.get(base, 0) + 1
    seen[base] = number
    return f"{base}.mp4" if number == 1 else f"{base} ({number}).mp4"


def selected_combinations(local: list[list[dict]], mode: str, requested: int, job_id: str):
    total_available = 1
    for stage in local:
        total_available *= len(stage)
    if mode == "random":
        wanted = min(max(1, requested), total_available)
        indices = random.Random(job_id).sample(range(total_available), wanted)
        sizes = [len(stage) for stage in local]
        selected: list[tuple[dict, ...]] = []
        for index in indices:
            pick: list[dict] = []
            for stage, size in zip(reversed(local), reversed(sizes)):
                pick.append(stage[index % size])
                index //= size
            selected.append(tuple(reversed(pick)))
        return selected, total_available, wanted
    if total_available > MAX_COMBINATIONS:
        raise RuntimeError(f"Este worker aceita no máximo {MAX_COMBINATIONS} combinações por job.")
    return itertools.product(*local), total_available, total_available


def process_job(job_id: str) -> None:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        workdir = Path(job["workdir"])
        stages = job["stages"]
        mode = job["mode"]
        requested = job["requested"]
        quality = job.get("quality", "camera4k")
        remove_silence_enabled = bool(job.get("remove_silence", False))
        silence_threshold = float(job.get("silence_threshold", 1.0))
        silence_padding = float(job.get("silence_padding", 0.15))

    try:
        inputs = workdir / "inputs"
        prepared = workdir / "prepared"
        outputs_dir = workdir / "outputs"
        shutil.rmtree(inputs, ignore_errors=True)
        shutil.rmtree(prepared, ignore_errors=True)
        shutil.rmtree(outputs_dir, ignore_errors=True)
        for old_zip in workdir.glob("download-*.zip"):
            old_zip.unlink(missing_ok=True)
        inputs.mkdir(exist_ok=True)
        prepared.mkdir(exist_ok=True)
        outputs_dir.mkdir(exist_ok=True)
        set_job(job_id, status="processing", phase="Preparando processamento", error=None, progress=0, overall_percent=1, outputs=[])

        raw: list[list[dict]] = []
        total_sources = sum(len(stage["files"]) for stage in stages)
        done = 0
        for stage_index, stage in enumerate(stages):
            stage_name = file_part(stage.get("name") or f"Etapa {stage_index + 1}")
            stage_items: list[dict] = []
            for file_index, remote in enumerate(stage["files"]):
                set_job(job_id, phase=f"Baixando vídeo {done + 1}/{total_sources}", overall_percent=2 + round(done / max(total_sources, 1) * 18, 1))
                ext = Path(remote.get("name", "video.mp4")).suffix or ".mp4"
                src = inputs / f"s{stage_index:02d}_f{file_index:03d}{ext}"
                download(remote["url"], src)
                stage_items.append({
                    "path": src,
                    "label": f"{stage_name} {file_index + 1}",
                    "stage": stage_name,
                    "index": file_index + 1,
                    "original_name": remote.get("name", "video.mp4"),
                })
                done += 1
            raw.append(stage_items)

        flat = [item for stage in raw for item in stage]
        set_job(job_id, phase="Analisando vídeos", overall_percent=20)
        probes = [probe(item["path"]) for item in flat]
        reference = probes[0] if probes else None
        exact = exact_compatible(probes)
        video_ok = video_compatible(probes)
        local: list[list[dict]] = []
        silence_removed_total = 0

        if remove_silence_enabled:
            prep_mode = "silence"
            preserve_geometry = quality == "camera4k" and video_ok
            done = 0
            for stage_index, stage_items in enumerate(raw):
                prepared_stage: list[dict] = []
                for file_index, item in enumerate(stage_items):
                    set_job(
                        job_id,
                        phase=f"Removendo silêncios {done + 1}/{total_sources}",
                        overall_percent=20 + round(done / max(total_sources, 1) * 35, 1),
                    )
                    dst = prepared / f"s{stage_index:02d}_f{file_index:03d}.mp4"
                    count = remove_silence(
                        item["path"], dst, quality, reference, probes[done] if done < len(probes) else None,
                        silence_threshold, silence_padding, preserve_geometry,
                    )
                    silence_removed_total += count
                    prepared_stage.append({**item, "path": dst})
                    item["path"].unlink(missing_ok=True)
                    done += 1
                local.append(prepared_stage)
            set_job(job_id, phase="Silêncios removidos", overall_percent=55)
        else:
            prep_mode = "copy" if exact else "audio" if video_ok else "normalize"
            if exact:
                local = raw
                set_job(job_id, phase="Fast Mode: qualidade original sem recodificar", overall_percent=28)
            else:
                done = 0
                for stage_index, stage_items in enumerate(raw):
                    prepared_stage = []
                    for file_index, item in enumerate(stage_items):
                        if prep_mode == "audio":
                            phase = f"Preservando vídeo e ajustando áudio {done + 1}/{total_sources}"
                            percent = 20 + round(done / max(total_sources, 1) * 18, 1)
                        else:
                            profile = PROFILES.get(quality, PROFILES["camera4k"])
                            phase = f"Preparando {profile['label']} {done + 1}/{total_sources}"
                            percent = 20 + round(done / max(total_sources, 1) * 30, 1)
                        set_job(job_id, phase=phase, overall_percent=percent)
                        dst = prepared / f"s{stage_index:02d}_f{file_index:03d}.mp4"
                        if prep_mode == "audio":
                            normalize_audio_only(item["path"], dst, quality)
                        else:
                            normalize(item["path"], dst, quality, reference)
                        prepared_stage.append({**item, "path": dst})
                        item["path"].unlink(missing_ok=True)
                        done += 1
                    local.append(prepared_stage)
                set_job(job_id, phase="Preparação concluída", overall_percent=40 if prep_mode == "audio" else 50)

        selected, total_available, count = selected_combinations(local, mode, requested, job_id)
        base = 55 if remove_silence_enabled else 28 if exact else 40 if prep_mode == "audio" else 50
        set_job(job_id, total=count, progress=0, phase="Gerando combinações", overall_percent=base)

        seen: dict[str, int] = {}
        output_meta: list[dict] = []
        for index, combo in enumerate(selected, 1):
            filename = output_name(combo, seen)
            out = outputs_dir / filename
            concat(combo, out)
            meta = {
                "file": filename,
                "size": out.stat().st_size,
                "duration": media_duration(out),
                "sources": [
                    {"label": item["label"], "stage": item["stage"], "index": item["index"], "original": item["original_name"]}
                    for item in combo
                ],
            }
            output_meta.append(meta)
            set_job(
                job_id,
                progress=index,
                phase=f"Gerando {index}/{count}",
                overall_percent=base + round(index / max(count, 1) * (100 - base), 1),
            )

        shutil.rmtree(inputs, ignore_errors=True)
        shutil.rmtree(prepared, ignore_errors=True)
        set_job(
            job_id,
            status="ready",
            phase="Pronto para baixar",
            progress=count,
            overall_percent=100,
            outputs=output_meta,
            preparation_mode=prep_mode,
            silence_cuts=silence_removed_total,
            reference_resolution=f"{reference[1]}x{reference[2]}" if reference else None,
            total_available=total_available,
        )
    except Exception as error:
        set_job(job_id, status="error", phase="Falha no processamento", error=str(error) or type(error).__name__)


def recover_interrupted() -> None:
    for path in ROOT.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            job_id = path.parent.name
            if time.time() - job.get("created_at", 0) > JOB_TTL_SECONDS:
                continue
            job["workdir"] = str(path.parent)
            with lock:
                jobs[job_id] = job
            if job.get("status") in ("queued", "processing"):
                set_job(job_id, status="queued", phase="Retomando após reinício do worker", overall_percent=0, error=None)
                executor.submit(process_job, job_id)
        except Exception:
            continue


def cleanup() -> None:
    while True:
        now = time.time()
        with lock:
            expired = [(job_id, job.get("workdir")) for job_id, job in jobs.items() if now - job["created_at"] > JOB_TTL_SECONDS]
        for job_id, workdir in expired:
            with lock:
                jobs.pop(job_id, None)
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
        time.sleep(600)


def output_path(job: dict, filename: str) -> Path:
    allowed = {item.get("file") for item in job.get("outputs", [])}
    if filename not in allowed:
        raise HTTPException(404, "Vídeo não encontrado neste job.")
    path = Path(job["workdir"]) / "outputs" / filename
    if not path.exists():
        raise HTTPException(404, "O vídeo não está mais disponível.")
    return path


def build_zip(job_id: str, job: dict, filenames: list[str], suffix: str) -> Path:
    if not filenames:
        raise HTTPException(400, "Selecione pelo menos um vídeo.")
    zip_path = Path(job["workdir"]) / f"download-{suffix}-{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for filename in filenames:
            path = output_path(job, filename)
            archive.write(path, arcname=filename)
        manifest = {
            "project": job.get("project_name"),
            "files": filenames,
            "generated_at": time.time(),
        }
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return zip_path


recover_interrupted()
threading.Thread(target=cleanup, daemon=True).start()


@app.get("/")
def root():
    return {"name": APP_NAME, "status": "ok", "version": "0.8.0"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "ffmpeg": Path(FFMPEG).name,
        "version": "0.8.0",
        "profiles": PROFILES,
        "copy_codecs": COPY_CODECS,
        "silence_thresholds": SILENCE_THRESHOLDS,
    }


@app.post("/v1/jobs/remote")
async def create_remote(request: Request):
    payload = await request.json()
    stages = payload.get("stages") or []
    if len(stages) < 2:
        raise HTTPException(400, "Adicione pelo menos duas etapas.")
    for index, stage in enumerate(stages):
        stage["name"] = str(stage.get("name") or f"Etapa {index + 1}").strip() or f"Etapa {index + 1}"
        if not stage.get("files"):
            raise HTTPException(400, f"A etapa '{stage['name']}' não possui vídeos.")
        for remote in stage["files"]:
            if not str(remote.get("url", "")).startswith("https://res.cloudinary.com/sskapqzv/"):
                raise HTTPException(400, "Origem de vídeo não permitida.")

    total = 1
    for stage in stages:
        total *= len(stage["files"])
    mode = payload.get("mode", "all")
    requested = int(payload.get("requested", 25))
    quality = payload.get("quality", "camera4k")
    if quality not in PROFILES:
        quality = "camera4k"
    remove_silence_enabled = bool(payload.get("removeSilence", False))
    try:
        silence_threshold = float(payload.get("silenceThreshold", 1.0))
    except (TypeError, ValueError):
        silence_threshold = 1.0
    if silence_threshold not in SILENCE_THRESHOLDS:
        silence_threshold = 1.0
    try:
        silence_padding = float(payload.get("silencePadding", 0.15))
    except (TypeError, ValueError):
        silence_padding = 0.15
    if silence_padding not in SILENCE_PADDINGS:
        silence_padding = 0.15
    if mode == "all" and total > MAX_COMBINATIONS:
        raise HTTPException(400, f"Há {total} combinações. O limite é {MAX_COMBINATIONS}.")

    job_id = uuid.uuid4().hex[:12]
    workdir = ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    with lock:
        jobs[job_id] = {
            "status": "queued",
            "phase": "Na fila",
            "progress": 0,
            "total": total if mode == "all" else min(requested, total),
            "overall_percent": 0,
            "error": None,
            "created_at": time.time(),
            "workdir": str(workdir),
            "stages": stages,
            "mode": mode,
            "requested": requested,
            "quality": quality,
            "remove_silence": remove_silence_enabled,
            "silence_threshold": silence_threshold,
            "silence_padding": silence_padding,
            "project_name": safe_name(payload.get("projectName", "Projeto VideoLabs")),
            "outputs": [],
        }
    persist(job_id)
    executor.submit(process_job, job_id)
    return public_job(job_id)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str):
    return public_job(job_id)


@app.get("/v1/jobs/{job_id}/outputs/{filename}/preview")
def preview_output(job_id: str, filename: str):
    job = get_job_or_404(job_id)
    if job.get("status") != "ready":
        raise HTTPException(409, "O processamento ainda não terminou.")
    return FileResponse(output_path(job, filename), media_type="video/mp4")


@app.get("/v1/jobs/{job_id}/outputs/{filename}/download")
def download_output(job_id: str, filename: str):
    job = get_job_or_404(job_id)
    if job.get("status") != "ready":
        raise HTTPException(409, "O processamento ainda não terminou.")
    path = output_path(job, filename)
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/v1/jobs/{job_id}/download")
def download_all(job_id: str):
    job = get_job_or_404(job_id)
    if job.get("status") != "ready":
        raise HTTPException(409, "O processamento ainda não terminou.")
    filenames = [item["file"] for item in job.get("outputs", [])]
    path = build_zip(job_id, job, filenames, "todos")
    filename = f"{job.get('project_name', 'VideoLabs')}-VideoLabs.zip"
    return FileResponse(path, media_type="application/zip", filename=filename, background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/v1/jobs/{job_id}/download-selected")
async def download_selected(job_id: str, request: Request):
    job = get_job_or_404(job_id)
    if job.get("status") != "ready":
        raise HTTPException(409, "O processamento ainda não terminou.")
    payload = await request.json()
    requested_files = [str(name) for name in (payload.get("files") or [])]
    allowed = {item["file"] for item in job.get("outputs", [])}
    filenames = [name for name in requested_files if name in allowed]
    if not filenames:
        raise HTTPException(400, "Selecione pelo menos um vídeo válido.")
    path = build_zip(job_id, job, filenames, "selecionados")
    filename = f"{job.get('project_name', 'VideoLabs')}-selecionados.zip"
    return FileResponse(path, media_type="application/zip", filename=filename, background=BackgroundTask(path.unlink, missing_ok=True))
