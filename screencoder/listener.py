"""Live transcript of everything the PC plays (WASAPI loopback) using a streaming sherpa-onnx model on the CPU."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .config import HOME_DIR

MODELS_DIR = HOME_DIR / "models"


@dataclass(frozen=True)
class SpeechModel:
    key: str
    label: str
    repo: str  # Hugging Face mirror maintained by the sherpa-onnx author
    encoder: str
    decoder: str
    joiner: str

    @property
    def folder(self) -> Path:
        return MODELS_DIR / self.repo.split("/")[-1]

    @property
    def files(self) -> tuple[str, ...]:
        return (self.encoder, self.decoder, self.joiner, "tokens.txt")

    def installed(self) -> bool:
        return all((self.folder / name).is_file() for name in self.files)


# Measured on this project's test clips (clean + call-quality speech, one CPU thread): Kroko needs ~0.04 s of
# compute per second of audio and adds punctuation/capitals; FastConformer needs 0.10-0.17 s, is lowercase
# without punctuation, and made slightly more mistakes on call-quality audio.
SPEECH_MODELS = {
    m.key: m
    for m in (
        SpeechModel(
            "kroko",
            "Kroko streaming Zipformer, English (~70 MB, lightest, punctuated - recommended)",
            "csukuangfj/sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06",
            "encoder.onnx", "decoder.onnx", "joiner.onnx",
        ),
        SpeechModel(
            "nemo-fastconformer",
            "NVIDIA FastConformer streaming, English (~140 MB, ~3x the CPU, no punctuation)",
            "csukuangfj/sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-480ms-int8",
            "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx",
        ),
    )
}
DEFAULT_SPEECH_MODEL = "kroko"

SILENCE_PEAK = 1e-4  # loopback delivers exact zeros when nothing is playing
SILENCE_FEED_SECS = 3.0  # keep feeding this much silence after sound so sentences can end
DEVICE_RECHECK_SECS = 8.0  # while silent, re-check which output device is the default


DOWNLOAD_SEGMENTS = 6  # parallel ranged connections per large file (slow links often throttle per connection)
SEGMENT_MIN_SIZE = 4 << 20
SLOW_WINDOW_SECS, SLOW_MIN_BYTES = 20.0, 40_000  # reconnect a segment that crawls below ~2 KB/s
DOWNLOAD_ATTEMPTS = 60


def _fetch_segment(url, part: Path, start: int, end: int, counter, should_stop) -> None:
    """Download bytes [start, end] into `part`, resuming and reconnecting until complete."""
    want = end - start + 1
    for attempt in range(DOWNLOAD_ATTEMPTS):
        have = part.stat().st_size if part.exists() else 0
        if have >= want:
            return
        try:
            request = urllib.request.Request(url, headers={"Range": f"bytes={start + have}-{end}"})
            with urllib.request.urlopen(request, timeout=20) as r:
                if r.status != 206:
                    raise OSError(f"server ignored the byte range (HTTP {r.status})")
                window_start, window_bytes = time.monotonic(), 0
                with open(part, "ab") as fh:
                    while have < want and (chunk := r.read(min(1 << 16, want - have))):
                        if should_stop():
                            raise InterruptedError("download cancelled")
                        fh.write(chunk)
                        have += len(chunk)
                        window_bytes += len(chunk)
                        counter(len(chunk))
                        if time.monotonic() - window_start > SLOW_WINDOW_SECS:
                            if window_bytes < SLOW_MIN_BYTES:
                                break  # throttled connection: a fresh one is usually much faster
                            window_start, window_bytes = time.monotonic(), 0
        except InterruptedError:
            raise
        except OSError:  # timeouts, resets, HTTP errors: resume after a short pause
            if attempt == DOWNLOAD_ATTEMPTS - 1:
                raise
            time.sleep(min(10, 1 + attempt))
    if (part.stat().st_size if part.exists() else 0) < want:
        raise OSError(f"could not download {url}")


def _download_file(url: str, target: Path, size: int, counter, should_stop) -> None:
    count = DOWNLOAD_SEGMENTS if size >= SEGMENT_MIN_SIZE * 2 else 1
    step = -(-size // count)
    ranges = [(i * step, min(size, (i + 1) * step) - 1) for i in range(count)]
    parts = [target.with_name(f"{target.name}.part{i}") for i in range(count)]
    errors: list[BaseException] = []

    def worker(part, start, end):
        try:
            _fetch_segment(url, part, start, end, counter, lambda: should_stop() or bool(errors))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p, s, e), daemon=True) for p, (s, e) in zip(parts, ranges)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise next((e for e in errors if not isinstance(e, InterruptedError)), errors[0])
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "wb") as out:
        for part in parts:
            with open(part, "rb") as fh:
                while block := fh.read(1 << 20):
                    out.write(block)
    if tmp.stat().st_size != size:
        tmp.unlink()
        raise OSError(f"{target.name}: size mismatch after download")
    tmp.replace(target)
    for part in parts:
        part.unlink(missing_ok=True)


def download_model(model: SpeechModel, progress, should_stop) -> None:
    """Fetch the model files: parallel ranged segments, resumable across restarts, stalls retried.

    progress(done_bytes, total_bytes) may be called from several threads; should_stop() aborts.
    """
    model.folder.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://huggingface.co/api/models/{model.repo}/tree/main", timeout=30) as r:
        sizes = {f["path"]: int(f.get("size") or 0) for f in json.load(r) if f.get("type") == "file"}
    missing = [n for n in model.files if n not in sizes]
    if missing:
        raise OSError(f"{model.repo} has no {', '.join(missing)}")
    total = sum(sizes[n] for n in model.files)
    lock = threading.Lock()
    done = [0]

    def counter(n: int) -> None:
        with lock:
            done[0] += n
            progress(done[0], total)

    # Count what earlier (interrupted) runs already fetched.
    for name in model.files:
        target = model.folder / name
        if target.is_file() and target.stat().st_size == sizes[name]:
            counter(sizes[name])
        else:
            counter(sum(p.stat().st_size for p in model.folder.glob(f"{name}.part*")))
    for name in model.files:
        target = model.folder / name
        if not (target.is_file() and target.stat().st_size == sizes[name]):
            url = f"https://huggingface.co/{model.repo}/resolve/main/{name}"
            _download_file(url, target, sizes[name], counter, should_stop)


class SystemAudioListener(QObject):
    """Runs capture + recognition on a background thread; results arrive as Qt signals."""

    partial = Signal(str)  # the sentence currently being spoken
    final = Signal(str, bool)  # a finished sentence; True when it was closed by cut()
    level = Signal(float)  # 0..1 loudness, for an activity meter
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, model: SpeechModel, threads: int = 1, parent=None):
        super().__init__(parent)
        self.model = model
        self.threads = threads
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cut = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._cut.clear()
        self._thread = threading.Thread(target=self._run, name="pc-audio-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None

    def cut(self) -> None:
        """Finish the current sentence now; it is emitted via final(text, True) (text may be empty)."""
        self._cut.set()

    # ------------------------------------------------------------------ worker thread
    def _run(self) -> None:
        try:
            if not self.model.installed():
                self.status.emit("Downloading the speech model (one time)…")
                download_model(self.model, self._report_download, self._stop.is_set)
            if self._stop.is_set():
                return
            self.status.emit("Loading the speech model…")
            recognizer = self._load_recognizer()
            self._listen(recognizer)
        except InterruptedError:
            self.status.emit("Not listening")
        except Exception as exc:  # surfaced in the UI; the app keeps working without audio
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")

    def _report_download(self, done: int, total: int) -> None:
        now = time.monotonic()
        if total and (now - getattr(self, "_last_report", 0.0) > 0.5 or done >= total):
            self._last_report = now
            self.status.emit(f"Downloading the speech model (one time)… {done * 100 // total}% "
                             f"({done / 1e6:.0f}/{total / 1e6:.0f} MB)")

    def _load_recognizer(self):
        import sherpa_onnx

        folder = self.model.folder
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(folder / "tokens.txt"),
            encoder=str(folder / self.model.encoder),
            decoder=str(folder / self.model.decoder),
            joiner=str(folder / self.model.joiner),
            num_threads=self.threads,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,  # nothing recognised yet
            rule2_min_trailing_silence=1.0,  # pause that ends a sentence
            rule3_min_utterance_length=30.0,  # force a break in very long monologues
        )

    def _open_loopback(self, pa, pyaudio, sink: queue.Queue):
        import numpy as np

        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        device = speakers if speakers.get("isLoopbackDevice") else None
        if device is None:
            for candidate in pa.get_loopback_device_info_generator():
                if speakers["name"] in candidate["name"]:
                    device = candidate
                    break
        if device is None:
            raise RuntimeError(f"No loopback capture available for {speakers['name']}")
        channels = max(1, int(device["maxInputChannels"]))
        rate = int(device["defaultSampleRate"])

        def callback(data, _frames, _time, _status):
            samples = np.frombuffer(data, dtype=np.float32)
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            sink.put(samples.astype(np.float32, copy=False))
            return (None, pyaudio.paContinue)

        stream = pa.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
            input_device_index=device["index"], frames_per_buffer=rate // 10, stream_callback=callback,
        )
        return stream, rate, speakers["name"]

    def _listen(self, recognizer) -> None:
        import numpy as np
        import pyaudiowpatch as pyaudio

        audio: queue.Queue = queue.Queue(maxsize=600)  # ~60 s of 100 ms chunks
        state = {"pa": None, "stream": None, "rate": 16000, "device": ""}

        def reopen() -> None:
            """(Re)open loopback on the current default output device.

            PortAudio only rescans devices when fully re-initialised, so this tears everything down;
            it runs only while nothing is playing (~30 ms), which is how headphone switches are followed.
            """
            self._close(state["pa"], state["stream"])
            state["pa"] = pyaudio.PyAudio()
            try:
                state["stream"], rate, name = self._open_loopback(state["pa"], pyaudio, audio)
            except Exception as exc:
                state["stream"] = None
                self.status.emit(f"Waiting for an audio output device… ({exc})")
                return
            while not audio.empty():  # drop leftovers recorded at the old device's sample rate
                audio.get_nowait()
            state["rate"] = rate
            if name != state["device"]:
                state["device"] = name
                self.status.emit(f"Listening to {name}")

        reopen()
        rec_stream = recognizer.create_stream()
        last_partial = ""
        last_sound = last_check = time.monotonic()
        level_shown = 0.0
        try:
            while not self._stop.is_set():
                if self._cut.is_set():
                    self._cut.clear()
                    text = self._text(recognizer, rec_stream)
                    recognizer.reset(rec_stream)
                    last_partial = ""
                    self.partial.emit("")
                    self.final.emit(text, True)
                try:
                    chunk = audio.get(timeout=0.1)
                except queue.Empty:
                    chunk = None
                now = time.monotonic()
                if chunk is not None and chunk.size:
                    peak = float(np.abs(chunk).max())
                    if peak or level_shown:
                        level_shown = min(1.0, peak * 1.5)
                        self.level.emit(level_shown)
                    if peak > SILENCE_PEAK:
                        last_sound = now
                    if peak > SILENCE_PEAK or last_partial or now - last_sound <= SILENCE_FEED_SECS:
                        rec_stream.accept_waveform(state["rate"], chunk)
                        while recognizer.is_ready(rec_stream):
                            recognizer.decode_stream(rec_stream)
                        text = self._text(recognizer, rec_stream)
                        if recognizer.is_endpoint(rec_stream):
                            recognizer.reset(rec_stream)
                            last_partial = ""
                            self.partial.emit("")
                            if text:
                                self.final.emit(text, False)
                        elif text != last_partial:
                            last_partial = text
                            self.partial.emit(text)
                        continue
                # Idle: nothing is playing and no sentence is open, so recognition is skipped (no CPU use).
                if level_shown:
                    level_shown = 0.0
                    self.level.emit(0.0)
                if now - max(last_sound, last_check) > DEVICE_RECHECK_SECS:
                    last_check = now
                    old_rate = state["rate"]
                    reopen()
                    if state["rate"] != old_rate:
                        rec_stream = recognizer.create_stream()
        finally:
            self._close(state["pa"], state["stream"])
            self.status.emit("Not listening")

    @staticmethod
    def _close(pa, stream) -> None:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except OSError:
            pass  # device already gone
        finally:
            if pa is not None:
                pa.terminate()

    @staticmethod
    def _text(recognizer, rec_stream) -> str:
        result = recognizer.get_result(rec_stream)
        return str(getattr(result, "text", result)).strip()


def supported() -> bool:
    return sys.platform == "win32"
