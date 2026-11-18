"""URL → verified video evidence → documents → optional bounded daily rules."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

from pydantic import ValidationError

from ai.providers.experiment_generation import ExperimentGenerator, GeneratedExperiment
from core.experiments import rules
from core.experiments.source_store import SourceStore
from core.experiments.source_urls import canonical_video_url
from core.experiments.store import ExperimentStore
from utils.logger import log

ROOT = Path(__file__).resolve().parents[2]
FILES = {".mp4", ".mkv", ".webm", ".jpg", ".json", ".txt", ".md", ".log", ".description"}
STAGES = {"downloading", "verifying", "sampling-frames", "transcribing", "evidence-ready"}


class ImportCancelled(RuntimeError):
    pass


def save(path: Path, value: object) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text)
    temporary.replace(path)


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def evidence_digest(directory: Path, evidence: dict) -> str:
    for sample in evidence["frames"]:
        for key in ("file", "review_file"):
            path = (directory / sample[key]).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file():
                raise ValueError("Invalid source frame path")
        if not 0 <= sample["seconds"] < evidence["duration"]:
            raise ValueError("Invalid source frame timestamp")
    video = (directory / evidence["media"]["file"]).resolve()
    if not video.is_relative_to(directory.resolve()) or not video.is_file():
        raise ValueError("Invalid source video path")
    if video.stat().st_size != evidence["media"]["bytes"] or digest(video) != evidence["media"]["sha256"]:
        raise ValueError("Prepared video integrity check failed")
    hashes = {sample["review_file"]:digest(directory / sample["review_file"]) for sample in evidence["frames"]}
    return hashlib.sha256(json.dumps({"evidence":evidence, "review_images":hashes},
                                     sort_keys=True, allow_nan=False).encode()).hexdigest()


def build_source(identity: str, directory: Path, evidence: dict,
                 generated: GeneratedExperiment, provenance: dict) -> dict:
    """A complete document is distinct from a runnable, compiler-valid strategy."""
    blockers = [*generated.missing_rules, *generated.unsupported_requirements, *generated.contradictions]
    if not generated.is_trading:
        blockers.append("This source does not supply a trading experiment")
    if generated.source_timeframe != "daily":
        blockers.append("The source does not establish a supported daily-equity timeframe")
    if not generated.source_complete:
        blockers.append("The generated interpretation identifies an incomplete source specification")
    if generated.frame_requests:
        blockers.append("Further visual evidence is needed to resolve the requested source moments")
    spec = None
    if generated.rule_spec is not None:
        try:
            spec = rules.RuleSpecification.model_validate(generated.rule_spec).model_dump()
        except ValidationError:
            blockers.append("Generated rules failed the supported daily-rule contract; inspect generation-response.json")
    else:
        blockers.append("No complete supported entry/exit rule specification was generated")
    if blockers:
        spec = None
    blockers = list(dict.fromkeys(blockers))
    source_evidence_hash = evidence_digest(directory, evidence)
    footer = (f"\n\n## Saved evidence and generation\n\n"
              f"Source: [{evidence['url']}]({evidence['url']}). "
              f"Verified duration: {evidence['duration']:.2f} seconds.\n\n"
              f"[Video]({evidence['media']['file']}) · [Caption](video.description) · "
              "[Transcript](transcript.txt) · [Frame samples](frames/contact-sheet.jpg) · "
              "[Evidence](evidence.json) · [Generation record](source.json)\n\n"
              "AI-generated from the complete saved caption/transcript and sampled original frames. "
              "Sampling does not establish inspection of every video frame. This is not an independent "
              "performance verification or a human-reviewed adapter.\n\n"
              f"Video SHA-256: `{evidence['media']['sha256']}`. "
              f"Evidence SHA-256: `{source_evidence_hash}`.\n")
    writeup = f"# {generated.title.strip()}\n\n{generated.writeup.strip()}{footer}"
    plan = f"# Integration plan: {generated.title.strip()}\n\n{generated.plan.strip()}\n\n"
    plan += f"## Generated runtime scope\n\nSource writeup SHA-256: `{hashlib.sha256(writeup.encode()).hexdigest()}`.\n\n"
    if spec:
        plan += ("The following data-only daily rules are supported by the experiment replay engine. "
                 "Their independent fixed-notional execution is a declared research account, "
                 "not a verified implementation of every source/account claim.\n\n```json\n" +
                 json.dumps(spec, indent=2) + "\n```\n")
    else:
        plan += "Runnable replay is blocked for these recorded reasons:\n\n" + "\n".join(f"- {b}" for b in blockers) + "\n"
    entry = {"id":identity, "number":0, "origin":"url", "source_url":evidence["url"],
             "title":generated.title.strip(), "overview":generated.overview, "gaps":"\n\n".join(blockers),
             "source_path":f"sources/{identity}/writeup.md", "plan_path":f"sources/{identity}/33ai-integration.md",
             "writeup":writeup, "plan":plan, "writeup_sha256":hashlib.sha256(writeup.encode()).hexdigest(),
             "plan_sha256":hashlib.sha256(plan.encode()).hexdigest(), "rule_spec":spec,
             "evidence_sha256":source_evidence_hash, "generation":provenance,
             "generated_at":datetime.now(timezone.utc).isoformat()}
    implementation = rules.definition(spec) if spec else {
        "availability":"blocked", "kind":"blocked", "scope":"Generated source and integration plan",
        "blocker":"; ".join(blockers), "parameters":{}, "assumptions":[], "limitations":[],
        "adapter_version":rules.VERSION, "source_strategy_implemented":False}
    entry.update(implementation)
    save(directory / "writeup.md", writeup)
    save(directory / "33ai-integration.md", plan)
    save(directory / "source.json", entry)
    if spec:
        save(directory / "rules.json", spec)
    return entry


class SourceImportService:
    def __init__(self, store: ExperimentStore, *, generator=None, preparer=None):
        self.jobs = SourceStore(store)
        self._generator = generator
        self._preparer = preparer
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._media_probe = None

    def generator(self):
        return self._generator or ExperimentGenerator()

    @staticmethod
    def media_python() -> Path:
        from config import settings
        path = Path(settings.experiments_media_python)
        return path if path.is_absolute() else ROOT / path

    def capabilities(self) -> dict:
        python = self.media_python()
        if self._preparer is not None:
            media = True
        elif self._media_probe and self._media_probe[0] == str(python) and time.monotonic() - self._media_probe[1] < 30:
            media = self._media_probe[2]
        else:
            media = False
            if python.is_file():
                try:
                    result = subprocess.run([str(python), "-c", "import importlib.util; "
                         "assert all(importlib.util.find_spec(n) for n in ('yt_dlp','faster_whisper','imageio_ffmpeg','PIL','numpy','av'))"],
                         capture_output=True, timeout=5)
                    media = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._media_probe = (str(python), time.monotonic(), media)
        ai = self.generator().available()
        reasons = []
        if not media:
            reasons.append("Install requirements-experiments.txt into .venv-experiments or configure EXPERIMENTS_MEDIA_PYTHON")
        if not ai:
            reasons.append(self.generator().unavailable_reason() + " for transcript and frame analysis")
        return {"media_ready":media, "generation_ready":ai, "ready":media and ai,
                "message":"Ready to generate from public video URLs" if not reasons else "; ".join(reasons),
                "supported_sites":["Facebook", "YouTube", "Vimeo", "Instagram", "TikTok", "X"]}

    def submit(self, url: str, *, background: bool = True) -> dict:
        url = canonical_video_url(url.strip() if isinstance(url, str) else url)
        job, created = self.jobs.create(url)
        if created:
            self._start(job["id"], background)
        return self.detail(job["id"])

    def retry(self, identity: str, *, background: bool = True) -> dict:
        self.jobs.retry(identity)
        self._start(identity, background)
        return self.detail(identity)

    def _start(self, identity: str, background: bool) -> None:
        if background:
            thread = threading.Thread(target=self.execute, args=(identity,), daemon=True,
                                      name=f"experiment-source-{identity[-8:]}")
            with self._lock:
                self._threads[identity] = thread
            thread.start()
        else:
            self.execute(identity)

    def detail(self, identity: str) -> dict:
        job = self.jobs.get(identity)
        directory = self.jobs.directory(identity)
        job["files"] = sorted(str(p.relative_to(directory)) for p in directory.rglob("*")
                              if p.is_file() and p.suffix in FILES and p.resolve().is_relative_to(directory.resolve()))
        if directory.joinpath("progress.json").is_file() and job["state"] == "preparing":
            try:
                stage = json.loads((directory / "progress.json").read_text()).get("stage")
                if stage in STAGES:
                    job["stage"] = stage
            except (OSError, ValueError):
                pass
        return job

    def artifact(self, identity: str, name: str) -> Path:
        directory = self.jobs.directory(identity).resolve()
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or path.suffix not in FILES:
            raise KeyError("Unknown experiment artifact")
        return path

    @staticmethod
    def _known(url: str) -> dict | None:
        from core.experiments.catalog import catalog, sources
        for source in sources():
            match = re.search(r"\]\((https?://[^)]+)\)", source["writeup"])
            if match:
                try:
                    if canonical_video_url(match[1]) == url:
                        summary = next(s for s in catalog() if s["id"] == source["id"])
                        return {**source, **summary}
                except ValueError:
                    continue
        return None

    def _reuse(self, identity: str, source: dict, directory: Path, attempt: int) -> None:
        save(directory / "writeup.md", source["writeup"])
        save(directory / "33ai-integration.md", source["plan"])
        save(directory / "source.json", {"reused_source":source["id"], "writeup_sha256":source["writeup_sha256"],
                                          "plan_sha256":source["plan_sha256"]})
        archive = (ROOT / "indicator-vides" / source["id"]).resolve()
        if archive.is_relative_to((ROOT / "indicator-vides").resolve()) and archive.is_dir():
            for item in archive.rglob("*"):
                if not item.is_file() or item.suffix not in FILES or not item.resolve().is_relative_to(archive):
                    continue
                target = directory / item.relative_to(archive)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(item, target)
                except OSError:
                    shutil.copyfile(item, target)
        self.jobs.finish(identity, source, existing=True, attempt=attempt)

    @contextmanager
    def _worker_slot(self, cancelled: threading.Event):
        self.jobs.root.mkdir(parents=True, exist_ok=True)
        with (self.jobs.root / "worker.lock").open("a") as handle:
            while True:
                if cancelled.is_set():
                    raise ImportCancelled()
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    cancelled.wait(0.5)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _prepare(self, directory: Path, url: str, cancelled: threading.Event, extra: list[float] | None = None) -> None:
        if self._preparer:
            self._preparer(directory, url, cancelled, extra)
            return
        python = self.media_python()
        if not python.is_file():
            raise ValueError("Media worker is unavailable. Install requirements-experiments.txt in .venv-experiments")
        command = [str(python), str(ROOT / "scripts/experiment_media.py"), "--directory", str(directory.resolve()),
                   "--url", url, "--model-root", str((self.jobs.root / "models").resolve())]
        if extra:
            command += ["--frames-at", *map(str, extra)]
        env = {"PATH":os.defpath, "LANG":"C.UTF-8", "PYTHONNOUSERSITE":"1",
               "HF_HOME":str((self.jobs.root / "models").resolve())}
        with (directory / "preparation.log").open("a") as output:
            process = subprocess.Popen(command, stdout=output, stderr=output, env=env, start_new_session=True)
            deadline = time.monotonic() + 3600
            try:
                while process.poll() is None:
                    if cancelled.wait(0.3):
                        raise ImportCancelled()
                    if time.monotonic() > deadline:
                        raise ValueError("Video preparation reached its one-hour time limit")
                if process.returncode:
                    raise ValueError("Video preparation failed. Inspect preparation.log; a public accessible copy may be needed")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def execute(self, identity: str) -> None:
        attempt = self.jobs.claim(identity)
        if attempt is None:
            return
        done, cancelled = threading.Event(), threading.Event()

        def beat():
            while not done.wait(10):
                try:
                    if not self.jobs.heartbeat(identity, attempt):
                        cancelled.set()
                        return
                except Exception:
                    cancelled.set()
                    log.exception("Experiments | source-generation heartbeat failed")
                    return

        heartbeat = threading.Thread(target=beat, daemon=True)
        heartbeat.start()
        directory = self.jobs.root / identity
        try:
            directory.mkdir(parents=True, exist_ok=True)
            job = self.jobs.get(identity)
            known = self._known(job["url"])
            if known:
                self._reuse(identity, known, directory, attempt)
                return
            with self._worker_slot(cancelled):
                if not self.jobs.heartbeat(identity, attempt):
                    raise ImportCancelled()
                generator = self.generator()
                if not generator.available():
                    raise ValueError(generator.unavailable_reason())
                self.jobs.update(identity, attempt=attempt, state="preparing", stage="downloading", message="Preparing video, transcript and original frames")
                self._prepare(directory, job["url"], cancelled)
                evidence = json.loads((directory / "evidence.json").read_text())
                evidence_digest(directory, evidence)
                if cancelled.is_set() or not self.jobs.heartbeat(identity, attempt):
                    raise ImportCancelled()
                self.jobs.update(identity, attempt=attempt, state="generating", stage="analyzing", message="Studying narration, caption and frames; writing experiment")
                generated, provenance = generator.generate(directory, evidence)
                if not self.jobs.heartbeat(identity, attempt):
                    raise ImportCancelled()
                passes = [provenance]
                save(directory / "generation-first-pass.json", {"content":generated.model_dump(), "provenance":provenance})
                if generated.frame_requests:
                    self.jobs.update(identity, attempt=attempt, state="preparing", stage="additional-frames", message="Inspecting additional source moments to resolve settings")
                    self._prepare(directory, job["url"], cancelled, generated.frame_requests)
                    evidence = json.loads((directory / "evidence.json").read_text())
                    evidence_digest(directory, evidence)
                    self.jobs.update(identity, attempt=attempt, state="generating", stage="analyzing", message="Checking additional frames and finalizing rules")
                    generated, provenance = generator.generate(directory, evidence)
                    passes.append(provenance)
                if cancelled.is_set() or not self.jobs.heartbeat(identity, attempt):
                    raise ImportCancelled()
                source = build_source(identity, directory, evidence, generated, {**provenance, "passes":passes})
                self.jobs.finish(identity, source, attempt=attempt)
        except ImportCancelled:
            pass
        except Exception as exc:
            # SDK/transport exceptions may contain URLs, request details or secrets.
            # Only authored validation messages are exposed to the browser.
            message = str(exc) if type(exc) is ValueError else f"Source generation failed ({type(exc).__name__}); saved evidence can be retried"
            if getattr(exc, "status_code", None) in {401, 403}:
                message = "AI provider authentication failed. Update the selected provider's key in Settings, then Retry"
            elif getattr(exc, "status_code", None) in {402, 429}:
                message = "AI provider billing or rate limit prevented generation. Check the selected provider account, then Retry"
            self.jobs.update(identity, attempt=attempt, state="blocked" if type(exc) is ValueError else "failed",
                             stage="failed", message=message, error=message)
            log.warning("Experiments | URL generation stopped: {}", type(exc).__name__)
        finally:
            done.set()
            heartbeat.join(timeout=1)
            with self._lock:
                if self._threads.get(identity) is threading.current_thread():
                    self._threads.pop(identity, None)
