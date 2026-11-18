"""Prepare one URL's bounded local evidence in the optional media environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAX_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 3600


def save(path: Path, data: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def stage(directory: Path, value: str) -> None:
    save(directory / "progress.json", {"stage": value})


def public_network() -> None:
    # Check the actual DNS lookup too, closing the gap between a URL preflight
    # lookup and the lookup used for its connection. No proxies or credentials.
    import ipaddress
    original = socket.getaddrinfo

    def resolve(*args, **kwargs):
        addresses = original(*args, **kwargs)
        if any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise OSError("Only public network addresses are supported")
        return addresses

    socket.getaddrinfo = resolve


def download(directory: Path, url: str, ffmpeg: str) -> tuple[Path, dict]:
    import yt_dlp
    from yt_dlp.networking import _urllib
    from core.experiments.source_urls import canonical_video_url, check_public_fetch

    url = canonical_video_url(url)
    public_network()
    original_redirect = _urllib.RedirectHandler

    class PublicRedirect(original_redirect):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check_public_fetch(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    _urllib.RedirectHandler = PublicRedirect

    class PublicDownloader(yt_dlp.YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            return super().build_request_director([_urllib.UrllibRH])

        def urlopen(self, request):
            check_public_fetch(request if isinstance(request, str) else
                               getattr(request, "url", None) or request.full_url)
            return super().urlopen(request)

    def duration_filter(info, *, incomplete=False):
        duration = info.get("duration")
        if info.get("is_live") or info.get("_type") in {"playlist", "multi_video"}:
            return "Use a single recorded video"
        if duration is not None and (duration <= 0 or duration > MAX_SECONDS):
            return "Videos must be no longer than one hour"

    def bounded(progress):
        size = progress.get("total_bytes") or progress.get("total_bytes_estimate") or progress.get("downloaded_bytes", 0)
        if size > MAX_BYTES:
            raise ValueError("Video exceeds the 512 MiB download limit")

    options = {"noplaylist": True, "playlist_items": "1", "quiet": True, "no_warnings": True,
               "socket_timeout": 25, "retries": 1, "fragment_retries": 1, "proxy": "",
               "enable_file_urls": False, "hls_prefer_native": True, "continuedl": True,
               "format": "bestvideo[height<=1080][protocol=https]+bestaudio[protocol=https]/best[protocol=https]/best[protocol=http]",
               "merge_output_format": "mp4", "ffmpeg_location": ffmpeg,
               "outtmpl": str(directory / "video.%(ext)s"), "writeinfojson": True,
               "writedescription": True, "max_filesize": MAX_BYTES,
               "match_filter": duration_filter, "progress_hooks": [bounded]}
    with PublicDownloader(options) as downloader:
        metadata = downloader.extract_info(url, download=True)
    if not metadata or metadata.get("_type") in {"playlist", "multi_video"}:
        raise ValueError("The URL did not provide a single usable video")
    candidates = [p for p in directory.glob("video.*") if p.suffix in {".mp4", ".webm", ".mkv"}
                  and ".f" not in p.name]
    if len(candidates) != 1:
        raise ValueError("The URL did not provide a completed video file")
    return candidates[0], metadata


def frame(directory: Path, video: Path, seconds: float, ffmpeg: str, *, name: str) -> dict:
    from PIL import Image
    target = directory / "frames" / name
    if target.exists():
        target.unlink()
    result = subprocess.run([ffmpeg, "-v", "error", "-protocol_whitelist", "file,pipe", "-ss", str(seconds),
                             "-i", str(video), "-frames:v", "1", "-q:v", "2", "-y", str(target)],
                            capture_output=True, timeout=45)
    if result.returncode or not target.exists():
        # Some source containers overstate their visual duration. Retain the
        # actual last decoded timestamp, never label an EOF frame as a later time.
        import av
        last = None
        with av.open(str(video), options={"protocol_whitelist":"file,pipe"}) as container:
            for decoded in container.decode(video=0):
                last = decoded
        if last is None:
            raise ValueError("A requested video frame could not be decoded")
        seconds = float(last.time or 0)
        target = target.with_name(target.stem + f"-actual-{seconds:.2f}s.jpg")
        last.to_image().save(target, quality=95)
    with Image.open(target) as original:
        preview = original.convert("RGB")
        preview.thumbnail((1568, 1568))
        preview.save(target.with_name("review-" + target.name), quality=88)
    return {"file": "frames/" + target.name, "review_file": "frames/review-" + target.name, "seconds": seconds}


def prepare(directory: Path, url: str, model_root: Path) -> None:
    import av
    import imageio_ffmpeg
    import numpy as np
    from PIL import Image, ImageDraw

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    report = directory / "evidence.json"
    if report.exists():
        saved = json.loads(report.read_text())
        video = directory / saved["media"]["file"]
        if video.exists() and video.stat().st_size == saved["media"]["bytes"]:
            with video.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() == saved["media"]["sha256"]:
                    return
    stage(directory, "downloading")
    video, metadata = download(directory, url, ffmpeg)
    stage(directory, "verifying")
    subprocess.run([ffmpeg, "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(video),
                    "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"],
                   check=True, capture_output=True, timeout=600)
    with av.open(str(video), options={"protocol_whitelist":"file,pipe"}) as container:
        stream = container.streams.video[0]
        duration = float(stream.duration * stream.time_base) if stream.duration else container.duration / 1_000_000
        audio_present = bool(container.streams.audio)
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS or video.stat().st_size > MAX_BYTES:
        raise ValueError("The completed video exceeds the one-hour or 512 MiB limit")
    with video.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    stage(directory, "sampling-frames")
    (directory / "frames").mkdir(exist_ok=True)
    count = min(48, max(12, math.ceil(duration / 4)))
    samples = []
    for index in range(count):
        seconds = round(max(0, duration - 1) * index / max(1, count - 1), 2)
        samples.append(frame(directory, video, seconds, ffmpeg, name=f"{index:02d}-{seconds:07.2f}s.jpg"))
    save(directory / "frames/index.json", samples)
    sheet = Image.new("RGB", (4 * 320, math.ceil(count / 4) * 360), "#151515")
    draw = ImageDraw.Draw(sheet)
    for index, sample in enumerate(samples):
        with Image.open(directory / sample["review_file"]) as original:
            picture = original.copy()
            picture.thumbnail((312, 330))
            x, y = index % 4 * 320, index // 4 * 360
            sheet.paste(picture, (x + (320 - picture.width) // 2, y + 26))
            draw.text((x + 5, y + 5), f"{sample['seconds']:.2f} s", fill="white")
    sheet.save(directory / "frames/contact-sheet.jpg", quality=90)
    stage(directory, "transcribing")
    segments = []
    language = None
    if audio_present:
        from faster_whisper import WhisperModel
        decoded = subprocess.run([ffmpeg, "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(video),
                                  "-vn", "-ar", "16000", "-ac", "1", "-f", "f32le", "-"],
                                 capture_output=True, check=True, timeout=240)
        audio = np.frombuffer(decoded.stdout, dtype=np.float32).copy()
        model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4,
                             download_root=str(model_root))
        stream, info = model.transcribe(audio, beam_size=5, vad_filter=True, condition_on_previous_text=False)
        segments = [{"start":s.start, "end":s.end, "text":s.text.strip()} for s in stream]
        language = info.language
    transcript = {"method":"faster-whisper small, CPU int8; machine transcription",
                  "audio_present":audio_present, "language":language, "segments":segments}
    save(directory / "transcript.json", transcript)
    (directory / "transcript.txt").write_text("Machine transcript; verify names and numbers against the source.\n" +
        "\n".join(f"[{s['start']:.2f}–{s['end']:.2f} s] {s['text']}" for s in segments) + "\n")
    description = metadata.get("description") or ""
    (directory / "video.description").write_text(description)
    save(report, {"url":url, "title":metadata.get("title") or "Video experiment",
                  "creator":metadata.get("uploader"), "duration":duration,
                  "media":{"file":video.name, "bytes":video.stat().st_size, "sha256":digest,
                           "verification":"full video/audio decode with ffmpeg -xerror"},
                  "description":description, "transcript":transcript, "frames":samples,
                  "sampling":"Uniform samples; additional requested moments may be added. Not every frame is inspected."})
    stage(directory, "evidence-ready")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--frames-at", type=float, nargs="+")
    args = parser.parse_args()
    import yt_dlp, imageio_ffmpeg, faster_whisper, PIL, numpy, av  # noqa: F401
    if args.check:
        print("Media worker ready")
        return
    if not args.directory or not args.directory.is_absolute():
        raise ValueError("An absolute experiment directory is required")
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.frames_at:
        saved = json.loads((args.directory / "evidence.json").read_text())
        if len(args.frames_at) > 12 or any(not math.isfinite(s) or not 0 <= s < saved['duration'] for s in args.frames_at):
            raise ValueError("Invalid additional frame timestamps")
        for index, seconds in enumerate(args.frames_at):
            saved["frames"].append(frame(args.directory, args.directory / saved["media"]["file"], seconds,
                                          imageio_ffmpeg.get_ffmpeg_exe(), name=f"detail-{index:02d}-{seconds:07.2f}s.jpg"))
        save(args.directory / "evidence.json", saved)
    else:
        prepare(args.directory, args.url, args.model_root)


if __name__ == "__main__":
    main()
