"""
即時上課翻譯＋筆記：核心邏輯（不含介面）

流程：
    麥克風 / 音訊檔
      → Voxtral Realtime（語音轉文字，串流）
      → 斷句（SentenceBuffer）
      → Mistral LLM 翻譯成繁體中文
      → 顯示在 Streamlit，並即時存檔
    下課後：
      → 把整堂逐字稿分段整理，產生 Markdown 筆記
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue as thread_queue
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

import numpy as np

SAMPLE_RATE = 16000          # Voxtral Realtime 需要 16 kHz、單聲道、16-bit PCM
CHUNK_MS = 100               # 每次送出 100 毫秒的音訊
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000
REALTIME_PRICE_PER_MIN = 0.006   # 美元／分鐘（2026 年官方公告價格，可能變動）

DEFAULT_STT_MODEL = "voxtral-mini-transcribe-realtime-2602"
DEFAULT_TRANSLATE_MODEL = "mistral-small-latest"
DEFAULT_NOTES_MODEL = "mistral-medium-latest"
DEFAULT_SLIDES_MODEL = "mistral-large-latest"   # 讀簡報只跑一次，用最強的模型


# ---------------------------------------------------------------------------
# 1. 斷句：把串流進來的零碎文字，組成適合翻譯的句子
# ---------------------------------------------------------------------------
_SENTENCE_END = re.compile(r"[.!?…。！？](?:[\"'”’)\]]*)\s")
# 句點前面是這些縮寫時不算句尾（英文＋法文，比對時不分大小寫）
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "cf", "fig", "no",
    "vol", "p", "pp", "ch", "approx", "jr", "sr", "inc", "ltd", "co", "u.s", "u.k",
    "m", "mme", "mlle", "ex", "env",
}


def _is_abbreviation(text: str, dot_pos: int) -> bool:
    """text[dot_pos] 是句點時，判斷它是不是縮寫（Dr.、e.g.）或人名縮寫（J. Smith）。"""
    if text[dot_pos] != ".":
        return False
    m = re.search(r"([A-Za-zÀ-ÿ.]+)$", text[:dot_pos])
    if not m:
        return False
    word = m.group(1).lstrip(".")
    return word.lower() in _ABBREVIATIONS or (len(word) == 1 and word.isupper())


class SentenceBuffer:
    """
    規則：
    - 遇到句尾標點（. ! ? …）而且後面接了空白，而且累積長度 >= min_chars → 送出
      （Dr.、e.g.、J. Smith 這類縮寫的句點不算句尾）
    - 沒有句號但已經累積 comma_seconds 秒或 comma_chars 個字元 → 在最後一個逗號處切開送出
      （語音辨識有時整段只標逗號，不這樣做的話要等老師停下來才會翻譯）
    - 累積超過 max_chars 還沒有句尾 → 在最後一個逗號或空白處切開送出
    - 一段時間沒有新文字（idle_seconds）→ 把剩下的送出（由外部呼叫 flush_if_idle）
    min_chars 可以把太短的句子合併，減少翻譯請求次數。
    """

    def __init__(self, min_chars: int = 80, max_chars: int = 320, idle_seconds: float = 3.0,
                 comma_seconds: float = 6.0, comma_chars: int = 160):
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.idle_seconds = idle_seconds
        self.comma_seconds = comma_seconds
        self.comma_chars = comma_chars
        self.buf = ""
        self.last_update = time.monotonic()
        self.buf_started = self.last_update      # 目前這段開始累積的時間

    def add(self, text: str) -> list[str]:
        now = time.monotonic()
        if not self.buf.strip():
            self.buf_started = now
        self.buf += text
        self.last_update = now
        out: list[str] = []
        while True:
            cut = self._find_cut(now)
            if cut is None:
                break
            piece, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
            self.buf_started = now
            if piece:
                out.append(piece)
        return out

    def _find_cut(self, now: float) -> Optional[int]:
        # 找「長度已經夠」之後的第一個句尾
        for m in _SENTENCE_END.finditer(self.buf):
            if m.end() >= self.min_chars and not _is_abbreviation(self.buf, m.start()):
                return m.end()
        # 沒有句號、但已經等太久或太長：在最後一個逗號處切
        if now - self.buf_started >= self.comma_seconds or len(self.buf) >= self.comma_chars:
            pos = max(self.buf.rfind(sep) for sep in (", ", "; ", ": "))
            if pos >= 0 and pos + 2 >= self.min_chars:
                return pos + 2
        # 太長了：在逗號或空白處切
        if len(self.buf) > self.max_chars:
            window = self.buf[: self.max_chars]
            for sep in (", ", "; ", ": ", " "):
                pos = window.rfind(sep)
                if pos > self.max_chars // 2:
                    return pos + len(sep)
            return self.max_chars
        return None

    def flush_if_idle(self, now: Optional[float] = None) -> Optional[str]:
        now = time.monotonic() if now is None else now
        if self.buf.strip() and now - self.last_update >= self.idle_seconds:
            return self.flush()
        return None

    def flush(self) -> Optional[str]:
        piece, self.buf = self.buf.strip(), ""
        return piece or None

    @property
    def pending(self) -> str:
        return self.buf


# ---------------------------------------------------------------------------
# 2. 資料結構
# ---------------------------------------------------------------------------
@dataclass
class Segment:
    idx: int
    elapsed: float            # 開始錄音後幾秒
    source: str               # 原文
    translation: Optional[str] = None   # None = 翻譯中
    error: Optional[str] = None


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _message_text(resp) -> str:
    """從 chat.complete 的回應取出文字（content 可能是字串或多段）。"""
    content = resp.choices[0].message.content
    if isinstance(content, str):
        return content.strip()
    parts = []
    for c in content or []:
        t = getattr(c, "text", None)
        if t is None and isinstance(c, dict):
            t = c.get("text")
        if t:
            parts.append(t)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# 3. 音訊來源
# ---------------------------------------------------------------------------
def load_wav_as_pcm16(path: str | Path) -> bytes:
    """讀 WAV 檔，轉成 16 kHz、單聲道、16-bit PCM（模擬上課用）。"""
    with wave.open(str(path), "rb") as w:
        n_ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError("Only 16-bit PCM WAV files are supported")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    if rate != SAMPLE_RATE:
        new_len = int(len(data) * SAMPLE_RATE / rate)
        data = np.interp(np.linspace(0, len(data) - 1, new_len), np.arange(len(data)), data)
    return np.clip(data, -32768, 32767).astype(np.int16).tobytes()


def list_input_devices() -> list[tuple[int, str]]:
    import sounddevice as sd
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


# ---------------------------------------------------------------------------
# 4. 即時翻譯 Session（在背景執行緒跑自己的 asyncio 迴圈）
# ---------------------------------------------------------------------------
TRANSLATE_SYSTEM = (
    "你是大學課堂的即時口譯員。把使用者給的課堂逐字稿片段（英文或法文）翻譯成自然、準確的繁體中文（台灣用語）。\n"
    "規則：\n"
    "1. 片段裡的每一句都要翻譯，包括開場、轉折的短句（例如 Let's start with a simple question.、Now, compare two models.），"
    "不可以省略、摘要或合併句子。不要加解釋、不要重複原文。\n"
    "2. 專有名詞、人名、理論名稱、機構名稱在翻譯中保留原文在括號內，例如：轉換型領導（transformational leadership）。"
    "只使用學術界通用的中文譯名；不確定有沒有通用譯名時，直接保留原文，不要自己創造譯名，也不要用成語或字面意思硬翻。\n"
    "3. 逐字稿是語音辨識結果，可能有錯字或斷句不完整；照字面合理翻譯，不要自行補充原文沒有的內容。\n"
    "4. 「前文」只是幫助你理解上下文：不要翻譯前文，也不要把前文的內容加進翻譯裡。\n"
    "5. 用 JSON 回覆，格式：{\"translation\": \"翻譯\", \"terms\": [{\"en\": \"原文\", \"zh\": \"你用的中文譯名\"}]}。"
    "terms 列出這個片段（不含前文）裡你附了原文的每一個名詞；zh 必須和 translation 裡用的譯名一致，保留原文沒翻的就填原文。沒有就給空陣列。"
)


def glossary_prompt(glossary: dict[str, str]) -> str:
    """把指定譯名加到系統提示後面。"""
    if not glossary:
        return ""
    lines = "\n".join(f"{en} = {zh}" for en, zh in glossary.items())
    return ("\n\n確認過的譯名（只是譯名對照，不是課堂內容）：原文是同樣意思時，一定要使用這裡的中文譯名；"
            "只有在原文明顯是另一個意思時（例如同一個字當一般用語），才依前後文翻譯。\n" + lines)


def background_prompt(brief: str) -> str:
    """把從簡報整理出的課程背景加到系統提示後面。"""
    if not brief.strip():
        return ""
    return (f"\n\n這堂課的背景（從上課簡報整理，只用來判斷領域和用詞，不是課堂內容）：{brief.strip()}\n"
            "用詞請依照這個領域的習慣；專有名詞的譯名以「確認過的譯名」為準。")


def merge_terms(suggested: dict[str, str], fixed: dict[str, str]) -> dict[str, str]:
    """合併簡報譯名和使用者修正的譯名；原文不分大小寫，使用者修正的優先。"""
    merged = {en.lower(): (en, zh) for en, zh in suggested.items()}
    merged.update({en.lower(): (en, zh) for en, zh in fixed.items()})
    return dict(merged.values())


def fix_brief(brief: str, suggested: dict[str, str], fixed: dict[str, str]) -> str:
    """課程背景裡如果用了被使用者修正掉的舊譯名，換成新譯名。"""
    old = {en.lower(): zh for en, zh in suggested.items()}
    for en, zh in fixed.items():
        if old.get(en.lower()) and old[en.lower()] != zh:
            brief = brief.replace(old[en.lower()], zh)
    return brief


def terms_in(text: str, terms: dict[str, str]) -> dict[str, str]:
    """只留原文有出現在 text 裡的名詞。"""
    low = text.lower()
    return {en: zh for en, zh in terms.items() if en.lower() in low}


def build_translate_messages(context: str, text: str, glossary: Optional[dict[str, str]] = None,
                             background: str = "") -> list[dict]:
    user = (f"前文（不用翻譯）：{context}\n\n" if context else "") + f"要翻譯的片段：\n{text}"
    system = TRANSLATE_SYSTEM + background_prompt(background) + glossary_prompt(glossary or {})
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_translation(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """解析翻譯模型的 JSON 回覆；模型沒照格式回時，整段當翻譯、沒有名詞。"""
    try:
        data = json.loads(raw)
        text = str(data["translation"]).strip()
        terms = [(str(t["en"]).strip(), str(t["zh"]).strip()) for t in data.get("terms") or []
                 if isinstance(t, dict) and t.get("en") and t.get("zh")]
        return text, terms
    except (ValueError, KeyError, TypeError):
        return raw.strip(), []


class TermBook:
    """
    專有名詞表：翻譯時自動收集「原文 → 譯名」。
    使用者修正過的譯名（fixed）會強制套用到之後的翻譯和筆記；
    沒修正過的也會在之後的翻譯沿用，讓同一個名詞前後譯名一致。
    """

    def __init__(self, fixed: Optional[dict[str, str]] = None, suggested: Optional[dict[str, str]] = None):
        """fixed：使用者修正過的譯名；suggested：從簡報找到的譯名（使用者修正的優先）。"""
        self.lock = threading.Lock()
        self._terms: dict[str, dict] = {}       # key = 原文小寫
        for en, zh in (suggested or {}).items():
            self.add_seen(en, zh)
        for en, zh in (fixed or {}).items():
            self.fix(en, zh)

    def add_seen(self, en: str, zh: str):
        with self.lock:
            self._terms.setdefault(en.lower(), {"en": en, "zh": zh, "fixed": False})

    def fix(self, en: str, zh: str):
        en, zh = en.strip(), zh.strip()
        if en and zh:
            with self.lock:
                self._terms[en.lower()] = {"en": en, "zh": zh, "fixed": True}

    def fixed(self) -> dict[str, str]:
        with self.lock:
            return {t["en"]: t["zh"] for t in self._terms.values() if t["fixed"]}

    def rows(self) -> list[dict]:
        with self.lock:
            return [dict(t) for t in self._terms.values()]

    def relevant(self, text: str) -> dict[str, str]:
        """只挑原文有出現在 text 裡的名詞，避免提示越來越長。"""
        low = text.lower()
        with self.lock:
            return {t["en"]: t["zh"] for k, t in self._terms.items() if k in low}


@dataclass
class SessionConfig:
    api_key: str
    out_dir: Path
    course: str = ""
    stt_model: str = DEFAULT_STT_MODEL
    translate_model: str = DEFAULT_TRANSLATE_MODEL
    target_delay_ms: Optional[int] = None
    input_device: Optional[int] = None       # None = 系統預設麥克風
    wav_path: Optional[str] = None           # 有值 = 用音訊檔模擬
    wav_speed: float = 1.0                   # 模擬播放速度
    min_chars: int = 80
    fixed_terms: dict = field(default_factory=dict)   # 使用者修正過的譯名 {原文: 中文}
    slide_brief: str = ""                              # 從簡報整理的課程背景
    slide_terms: dict = field(default_factory=dict)   # 從簡報找到的譯名 {原文: 中文}
    save_audio: bool = True                  # 同時把錄音存成 FLAC（無損壓縮）


class LiveSession:
    def __init__(self, cfg: SessionConfig, client_factory: Optional[Callable] = None):
        self.cfg = cfg
        self._client_factory = client_factory
        self.lock = threading.Lock()
        self.segments: list[Segment] = []
        self.partial = ""
        self.language: Optional[str] = None
        self.status = "Not started"
        self.errors: list[str] = []
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.audio_seconds = 0.0
        self.running = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._buffer = SentenceBuffer(min_chars=cfg.min_chars)
        self._audio_q: "thread_queue.Queue[bytes]" = thread_queue.Queue()
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = cfg.out_dir / "逐字稿_雙語.md"
        self.source_path = cfg.out_dir / "逐字稿_原文.txt"
        self.audio_path = cfg.out_dir / "錄音.flac"
        self._audio_file = None
        self._caffeinate: Optional[subprocess.Popen] = None
        self._producer: Optional[threading.Thread] = None
        self._written_upto = 0
        self.terms = TermBook(cfg.fixed_terms, cfg.slide_terms)

    # ----- 對外介面 -----
    def start(self):
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self.started_at = time.time()
        with open(self.transcript_path, "w", encoding="utf-8") as f:
            title = self.cfg.course or "上課逐字稿"
            f.write(f"# {title}\n\n錄音開始：{datetime.now():%Y-%m-%d %H:%M}\n\n")
        self.source_path.write_text("", encoding="utf-8")
        if self.cfg.save_audio:
            try:
                import soundfile as sf
                self._audio_file = sf.SoundFile(self.audio_path, "w", samplerate=SAMPLE_RATE, channels=1,
                                                format="FLAC", subtype="PCM_16")
            except Exception as e:
                self._log_error(f"Could not create the audio file; saving the transcript only: {e}")
        self._keep_awake()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def _keep_awake(self):
        """錄音期間不讓 Mac 睡眠、螢幕也不自動關（-d 螢幕、-i 系統閒置；-w 程式結束就自動解除）。"""
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            try:
                self._caffeinate = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
            except Exception as e:
                self._log_error(f"Couldn't keep the Mac awake; it may sleep during class: {e}")

    def stop(self):
        self._stop.set()
        self._set_status("Stopping… (finishing the last lines)")

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.ended_at or time.time()) - self.started_at

    def est_cost(self) -> float:
        return self.audio_seconds / 60 * REALTIME_PRICE_PER_MIN

    def snapshot(self):
        with self.lock:
            return (list(self.segments), self._buffer.pending, self.status, list(self.errors), self.language)

    def full_source_text(self) -> str:
        with self.lock:
            return "\n".join(f"[{fmt_time(s.elapsed)}] {s.source}" for s in self.segments)

    # ----- 內部 -----
    def _set_status(self, s: str):
        with self.lock:
            self.status = s

    def _log_error(self, msg: str):
        with self.lock:
            self.errors.append(f"{datetime.now():%H:%M:%S} {msg}")

    def _make_client(self):
        if self._client_factory:
            return self._client_factory(self.cfg.api_key)
        from mistralai.client import Mistral
        return Mistral(api_key=self.cfg.api_key)

    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except Exception as e:  # 最後防線
            self._log_error(f"Unexpected error: {e!r}")
        finally:
            # 錄音檔由收音執行緒負責關；收音執行緒沒啟動（例如一開始就出錯）才在這裡關
            if self._producer is None and self._audio_file is not None:
                self._audio_file.close()
                self._audio_file = None
            if self._caffeinate is not None:
                self._caffeinate.terminate()
                self._caffeinate = None
            self.running = False
            self.ended_at = time.time()
            self._set_status("Stopped")

    async def _main(self):
        client = self._make_client()
        self._trans_q: asyncio.Queue = asyncio.Queue()
        producer = self._producer = threading.Thread(target=self._audio_producer, daemon=True)
        producer.start()
        translator = asyncio.create_task(self._translator(client))
        idle = asyncio.create_task(self._idle_flusher())
        stt = asyncio.create_task(self._stt_loop(client))
        stop_seen: Optional[float] = None
        try:
            # 按下停止後，最多等 20 秒讓伺服器送回最後的辨識結果
            while not stt.done():
                await asyncio.sleep(0.2)
                if self._stop.is_set():
                    stop_seen = stop_seen or time.monotonic()
                    if time.monotonic() - stop_seen > 20:
                        stt.cancel()
                        break
            try:
                await stt
            except asyncio.CancelledError:
                pass
        finally:
            idle.cancel()
            rest = self._buffer.flush()
            if rest:
                self._add_segment(rest)
            await self._trans_q.put(None)       # 通知翻譯結束
            await translator
            self._write_pending()
            # 等收音執行緒把最後的音訊寫進錄音檔、關檔
            await asyncio.get_running_loop().run_in_executor(None, producer.join, 10)

    # 音訊：麥克風或檔案 → 錄音檔 ＋ thread queue
    # 錄音檔在這裡寫，所以網路斷掉、還沒送去辨識的音訊也會先存進錄音檔
    def _audio_producer(self):
        try:
            if self.cfg.wav_path:
                pcm = load_wav_as_pcm16(self.cfg.wav_path)
                step = CHUNK_SAMPLES * 2
                for i in range(0, len(pcm), step):
                    if self._stop.is_set():
                        break
                    self._emit_audio(pcm[i:i + step])
                    time.sleep(CHUNK_MS / 1000 / max(self.cfg.wav_speed, 0.1))
                self._stop.set()     # 檔案播完就自動停止
            else:
                self._mic_loop()
        except Exception as e:
            self._log_error(f"Audio input failed: {e}")
            self._stop.set()
        finally:
            if self._audio_file is not None:
                self._audio_file.close()
                self._audio_file = None

    def _emit_audio(self, chunk: bytes):
        self._save_audio(chunk)
        self._audio_q.put(chunk)

    def _mic_loop(self):
        """
        收麥克風，出問題就自動重新接上，直到按停止為止：
        - 開不了、或錄到一半出錯 → 等一下再開
        - 3 秒都沒收到聲音（例如拔掉外接麥克風）→ 當作斷線，重新開
        - 指定的麥克風連續失敗 3 次 → 改用系統預設麥克風
        """
        import sounddevice as sd
        raw_q: "thread_queue.Queue[bytes]" = thread_queue.Queue()
        device, failures = self.cfg.input_device, 0

        def cb(indata, frames, t, status):
            raw_q.put(bytes(indata))

        while not self._stop.is_set():
            try:
                with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                       blocksize=CHUNK_SAMPLES, device=device, callback=cb) as stream:
                    last_data = time.monotonic()
                    while not self._stop.is_set():
                        try:
                            self._emit_audio(raw_q.get(timeout=0.2))
                        except thread_queue.Empty:
                            if not stream.active or time.monotonic() - last_data > 3:
                                raise RuntimeError("no sound from the microphone for 3 seconds")
                            continue
                        last_data = time.monotonic()
                        if failures:
                            self._log_error("Microphone reconnected")
                            self._set_status("Recording")
                            failures = 0
            except Exception as e:
                if self._stop.is_set():
                    break
                failures += 1
                self._log_error(f"Microphone problem (attempt {failures}): {e}")
                self._set_status("Microphone disconnected — reconnecting…")
                if device is not None and failures >= 3:
                    self._log_error("Switching to the system default microphone")
                    device = None
                self._stop.wait(min(failures, 5))   # 等的時候按停止也能馬上結束
                try:   # 重新整理裝置清單，才找得到重新插上的麥克風
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
        while not raw_q.empty():
            self._emit_audio(raw_q.get_nowait())

    async def _audio_stream(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        while True:
            try:
                chunk = await loop.run_in_executor(None, self._audio_q.get, True, 0.2)
            except thread_queue.Empty:
                if self._stop.is_set():
                    return
                continue
            self.audio_seconds += len(chunk) / 2 / SAMPLE_RATE
            yield chunk

    def _save_audio(self, chunk: bytes):
        """收到的每一段音訊寫進錄音檔（在收音執行緒呼叫，跟網路狀況無關）。"""
        if self._audio_file is None:
            return
        try:
            self._audio_file.write(np.frombuffer(chunk, dtype=np.int16))
        except Exception as e:
            self._log_error(f"Writing the audio file failed; saving the transcript only from now on: {e}")
            self._audio_file = None

    async def _stt_loop(self, client):
        from mistralai.client.models import (AudioFormat, RealtimeTranscriptionError,
                                             TranscriptionStreamDone, TranscriptionStreamLanguage,
                                             TranscriptionStreamTextDelta)
        fmt = AudioFormat(encoding="pcm_s16le", sample_rate=SAMPLE_RATE)
        failures = 0
        while True:
            self._set_status("Connecting…")
            try:
                kwargs = dict(audio_stream=self._audio_stream(), model=self.cfg.stt_model, audio_format=fmt)
                if self.cfg.target_delay_ms:
                    kwargs["target_streaming_delay_ms"] = self.cfg.target_delay_ms
                async for ev in client.audio.realtime.transcribe_stream(**kwargs):
                    if isinstance(ev, TranscriptionStreamTextDelta):
                        failures = 0
                        if not self._stop.is_set():
                            self._set_status("Recording")
                        with self.lock:
                            pieces = self._buffer.add(ev.text)
                        for p in pieces:
                            self._add_segment(p)
                    elif isinstance(ev, TranscriptionStreamLanguage):
                        with self.lock:
                            self.language = ev.audio_language
                    elif isinstance(ev, TranscriptionStreamDone):
                        break
                    elif isinstance(ev, RealtimeTranscriptionError):
                        msg = ev.error.message
                        self._log_error(f"Speech recognition error: {getattr(msg, 'detail', msg)}")
                    elif getattr(ev, "type", None) in ("session.created", "session.updated"):
                        if not self._stop.is_set():
                            self._set_status("Recording")
            except Exception as e:
                # 沒按停止就一直重連（例如教室 Wi-Fi 斷了一陣子）；斷線期間的音訊會排隊，連上後一起補送
                failures += 1
                self._log_error(f"Connection lost (attempt {failures}): {e}")
                self._set_status(f"Connection lost — reconnecting (attempt {failures})…")
                for _ in range(min(2 * failures, 10) * 5):
                    if self._stop.is_set():
                        break
                    await asyncio.sleep(0.2)
            if self._stop.is_set():
                return
            # 伺服器結束了這次連線但使用者沒按停止 → 自動重連

    async def _idle_flusher(self):
        while True:
            await asyncio.sleep(0.5)
            with self.lock:
                piece = self._buffer.flush_if_idle()
            if piece:
                self._add_segment(piece)

    def _add_segment(self, text: str):
        with self.lock:
            seg = Segment(idx=len(self.segments), elapsed=self.audio_seconds, source=text)
            self.segments.append(seg)
        with open(self.source_path, "a", encoding="utf-8") as f:
            f.write(f"[{fmt_time(seg.elapsed)}] {text}\n")
        self._trans_q.put_nowait(seg)

    async def _translator(self, client):
        while True:
            seg = await self._trans_q.get()
            if seg is None:
                return
            with self.lock:
                context = " ".join(s.source for s in self.segments[max(0, seg.idx - 2):seg.idx])
            for attempt in range(3):
                try:
                    glossary = self.terms.relevant(f"{context} {seg.source}")
                    brief = fix_brief(self.cfg.slide_brief, self.cfg.slide_terms, self.terms.fixed())
                    resp = await self._client_chat(client, self.cfg.translate_model,
                                                   build_translate_messages(context, seg.source, glossary, brief),
                                                   temperature=0.2, response_format={"type": "json_object"})
                    text, terms = parse_translation(_message_text(resp))
                    for en, zh in terms:
                        self.terms.add_seen(en, zh)
                    with self.lock:
                        seg.translation = text
                    break
                except Exception as e:
                    wait = 5 * (attempt + 1) if "429" in str(e) else 1.5 * (attempt + 1)
                    if attempt == 2:
                        with self.lock:
                            seg.error = f"Translation failed: {e}"
                            seg.translation = ""
                        self._log_error(f"Translation failed: {e}")
                    else:
                        await asyncio.sleep(wait)
            self._write_pending()

    @staticmethod
    async def _client_chat(client, model, messages, **kw):
        return await client.chat.complete_async(model=model, messages=messages, **kw)

    def _write_pending(self):
        """依序把已翻譯好的段落寫入雙語逐字稿（避免順序錯亂）。"""
        with self.lock:
            ready = []
            while self._written_upto < len(self.segments) and self.segments[self._written_upto].translation is not None:
                ready.append(self.segments[self._written_upto])
                self._written_upto += 1
        if ready:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                for s in ready:
                    f.write(f"**[{fmt_time(s.elapsed)}]** {s.source}\n\n> {s.translation or '（翻譯失敗）'}\n\n")


# ---------------------------------------------------------------------------
# 5. 下課後產生筆記
# ---------------------------------------------------------------------------
NOTES_CHUNK_SYSTEM = (
    "你是研究生的上課筆記助理。以下是一段課堂語音辨識逐字稿（英文或法文，含時間戳）。"
    "請用繁體中文（台灣用語）整理這一段的詳細筆記。\n"
    "要求：\n"
    "- 只根據逐字稿內容，不要加入逐字稿沒有的資訊（課程背景和譯名對照只用來決定用詞）；"
    "聽不清楚或不確定的地方標註「（逐字稿不清楚）」。\n"
    "- 依主題分小節，保留老師舉的例子、數字、人名、理論名稱（附原文）。\n"
    "- 老師提到的作業、考試、截止日、課前準備，全部列出並附時間戳。\n"
    "- 最後列出這段出現的專有名詞（原文｜中文｜一句話解釋）。\n"
    "- 專有名詞只用學術界通用的中文譯名；不確定有沒有通用譯名時，中文欄直接寫原文，不要自己創造譯名，也不要用成語或字面意思硬翻。"
)

NOTES_MERGE_SYSTEM = (
    "你是研究生的上課筆記助理。以下是同一堂課依時間順序分段整理的筆記。"
    "請合併成一份完整、詳細的繁體中文（台灣用語）Markdown 筆記。\n"
    "結構：\n"
    "# 課程名稱與日期\n"
    "## 一、本堂課摘要（5–8 句）\n"
    "## 二、詳細筆記（依主題分節，保留例子、數字、理論名稱原文）\n"
    "## 三、作業／考試／課前準備／重要提醒（附時間戳；沒有就寫「本堂課未提到」）\n"
    "## 四、專有名詞表（表格：原文｜中文｜解釋）\n"
    "## 五、逐字稿中不清楚、需要再確認的地方\n"
    "規則：只根據提供的內容，不要自行補充；重複內容要合併，不要遺漏。"
    "課程背景和譯名對照只用來決定用詞，裡面有但逐字稿沒講到的東西不要寫進筆記。\n"
    "同一個專有名詞全篇只用一種譯名；各段譯名不同時，選學術界通用的那個，不確定就保留原文。"
)


def _tidy_markdown(text: str) -> str:
    """把模型輸出中連續重複的分隔線（---）合併成一條。"""
    return re.sub(r"(?:^---[ \t]*\n\s*){2,}", "---\n\n", text, flags=re.M)


def chunk_text(text: str, max_chars: int = 12000) -> list[str]:
    lines, chunks, cur = text.splitlines(), [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > max_chars:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def generate_notes(api_key: str, transcript: str, course: str, date_str: str,
                   model: str = DEFAULT_NOTES_MODEL, client=None,
                   progress: Optional[Callable[[str], None]] = None,
                   cache_dir: Optional[Path] = None, retries: int = 4,
                   glossary: Optional[dict[str, str]] = None, background: str = "") -> str:
    """
    分段整理逐字稿，再合併成一份筆記。
    cache_dir：每段整理好就先存檔；中途失敗後再按一次，已完成的段落直接沿用，不會重複扣費。
    glossary：要照用的譯名（使用者修正過的、從簡報找到的）。background：從簡報整理的課程背景。
    """
    if client is None:
        from mistralai.client import Mistral
        client = Mistral(api_key=api_key)
    say = progress or (lambda s: None)
    chunks = chunk_text(transcript)
    if not chunks:
        return "(No transcript content)"

    def ask(system, user):
        for attempt in range(retries):
            try:
                resp = client.chat.complete(model=model, temperature=0.2,
                                            messages=[{"role": "system", "content": system},
                                                      {"role": "user", "content": user}])
                return _message_text(resp)
            except Exception as e:
                # 金鑰無效、沒有權限：重試也沒用，直接回報
                if attempt == retries - 1 or "401" in str(e) or "403" in str(e):
                    raise
                wait = 10 * (attempt + 1) if "429" in str(e) else 3 * (attempt + 1)
                say(f"⚠️ Request failed, retrying in {wait}s (attempt {attempt + 1}): {e}")
                time.sleep(wait)

    # 只給逐字稿裡真的出現過的名詞，避免模型把簡報上的名詞當成上課內容寫進筆記
    extra = background_prompt(background) + glossary_prompt(terms_in(transcript, glossary or {}))
    chunk_system = NOTES_CHUNK_SYSTEM + extra
    merge_system = NOTES_MERGE_SYSTEM + extra

    def cache_file(chunk: str) -> Optional[Path]:
        if cache_dir is None:
            return None
        key = hashlib.sha256(f"{model}\n{chunk_system}\n{chunk}".encode("utf-8")).hexdigest()[:16]
        return cache_dir / f"{key}.md"

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    partials = []
    for i, c in enumerate(chunks, 1):
        f = cache_file(c)
        if f is not None and f.exists():
            say(f"Part {i}/{len(chunks)} was already done, reusing it")
            partials.append(f.read_text(encoding="utf-8"))
            continue
        say(f"Summarizing part {i}/{len(chunks)}…")
        part = ask(chunk_system, c)
        if f is not None:
            f.write_text(part, encoding="utf-8")
        partials.append(part)
    say("Merging into the final notes…")
    joined = "\n\n---\n\n".join(f"【第 {i} 段】\n{p}" for i, p in enumerate(partials, 1))
    return _tidy_markdown(ask(merge_system, f"課程名稱：{course or '（未填）'}\n日期：{date_str}\n\n{joined}"))


# ---------------------------------------------------------------------------
# 6. 上課前讀簡報：整理課程背景和專有名詞
# ---------------------------------------------------------------------------
SLIDES_SYSTEM = (
    "你是大學課程助教。以下是一堂課的簡報文字（英文或法文）。用 JSON 回覆，格式：\n"
    "{\"summary\": \"…\", \"terms\": [{\"en\": \"原文\", \"zh\": \"繁體中文譯名\"}]}\n"
    "summary：用繁體中文（台灣用語）2–3 句說明這堂課的學科領域、主題和重點概念。\n"
    "terms：列出簡報裡的專有名詞、理論、概念、人名、機構名，最多 80 個。規則：\n"
    "- 只列完整的專業名詞（例如 merit goods、arm's length principle），不要列 board、goods 這種一般單字\n"
    "- zh 用台灣學術界通用的譯名；人名、機構名沒有通用譯名就保留原文；不確定的也保留原文，不要自己創造譯名"
)
SLIDES_MAX_CHARS = 40000


def extract_slide_text(filename: str, data: bytes) -> str:
    """從 PDF 或 PPTX 簡報取出文字。"""
    import io
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(data)).pages
        return "\n\n".join(f"[第 {i} 頁]\n{p.extract_text() or ''}" for i, p in enumerate(pages, 1))
    if name.endswith(".pptx"):
        from pptx import Presentation
        out = []
        for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
            texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                texts.append("備註：" + slide.notes_slide.notes_text_frame.text)
            out.append(f"[第 {i} 頁]\n" + "\n".join(texts))
        return "\n\n".join(out)
    raise ValueError("Only PDF or PPTX files are supported")


_PAGE_MARK = re.compile(r"\[第 \d+ 頁\]")


def limit_slide_text(text: str, max_chars: int = SLIDES_MAX_CHARS) -> tuple[str, str]:
    """
    簡報文字太長時，只留前面完整的幾頁（不切到半頁）。
    回傳（要送出的文字, 提醒訊息）；沒有超過就回傳原文和空字串。
    """
    if len(text) <= max_chars:
        return text, ""
    total = len(_PAGE_MARK.findall(text))
    cut = text[:max_chars]
    last_page = cut.rfind("\n\n[第 ")
    if last_page > 0:
        cut = cut[:last_page]
    read = len(_PAGE_MARK.findall(cut))
    return cut, f"Slides too long: only the first {read} of {total} slides were read. Terms on later slides won't be picked up automatically."


def analyze_slides(api_key: str, text: str, model: str = DEFAULT_SLIDES_MODEL,
                   client=None) -> tuple[str, dict[str, str], str]:
    """讀簡報文字，回傳（課程背景摘要, {原文: 中文譯名}, 提醒訊息）。"""
    if not text.strip():
        raise ValueError("No text found in the slides (they may be scanned images)")
    text, warning = limit_slide_text(text)
    if client is None:
        from mistralai.client import Mistral
        client = Mistral(api_key=api_key)
    resp = client.chat.complete(model=model, temperature=0.1, response_format={"type": "json_object"},
                                messages=[{"role": "system", "content": SLIDES_SYSTEM},
                                          {"role": "user", "content": text}])
    data = json.loads(_message_text(resp))
    terms = {str(t["en"]).strip(): str(t["zh"]).strip() for t in data.get("terms") or []
             if isinstance(t, dict) and t.get("en") and t.get("zh")}
    return str(data.get("summary", "")).strip(), terms, warning
