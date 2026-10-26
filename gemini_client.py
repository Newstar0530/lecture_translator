"""
Gemini 轉接層：把 Google Gemini API 包成跟 Mistral SDK 一樣的介面

core.py / retranscribe.py 只用到 Mistral client 的三個部分：
    client.chat.complete(...) / client.chat.complete_async(...)         翻譯、筆記、讀簡報
    client.audio.realtime.transcribe_stream(audio_stream=..., ...)      即時辨識（回傳 Mistral 的事件物件）
    client.audio.transcriptions.complete(...)                           下課後整段辨識
這裡用 Gemini 做出同樣的東西，所以選 Gemini 時主流程（重連、卡死偵測、研究紀錄）完全不用改。
"""
from __future__ import annotations

import asyncio
import io
import re
import tempfile
import time
import types as pytypes
from pathlib import Path
from typing import AsyncIterator, Optional

# 每個功能預設用的 Gemini 模型（ai.google.dev 官方價格頁上有列的型號）
GEMINI_LIVE_STT_MODEL = "gemini-3.5-transcribe-live"
GEMINI_FILE_STT_MODEL = "gemini-3.5-transcribe"
GEMINI_TRANSLATE_MODEL = "gemini-3.5-flash-lite"
GEMINI_NOTES_MODEL = "gemini-3.6-flash"
GEMINI_SLIDES_MODEL = "gemini-3.6-flash"

LIVE_SESSION_SECONDS = 570      # Gemini Live 一條連線最多 10 分鐘：9 分 30 秒就主動換新連線
LIVE_FINAL_WAIT = 4.0           # 換連線前，送出「聲音結束」後最多再等幾秒拿最後的辨識結果
BCP47 = {"en": "en-US", "fr": "fr-FR"}


# ---------------------------------------------------------------------------
# 文字：模仿 Mistral 的 chat.complete 回傳格式（resp.choices[0].message.content）
# ---------------------------------------------------------------------------
def _to_gemini(messages: list[dict]) -> tuple[str, str]:
    """Mistral 的 messages（system + user）換成 Gemini 的 system_instruction + 內容。"""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    return system, user


def _fake_response(text: str):
    msg = pytypes.SimpleNamespace(content=text)
    return pytypes.SimpleNamespace(choices=[pytypes.SimpleNamespace(message=msg)])


class _Chat:
    def __init__(self, client):
        self._client = client

    def _config(self, system: str, temperature: Optional[float], response_format: Optional[dict]):
        from google.genai import types
        return types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=temperature,
            response_mime_type="application/json" if (response_format or {}).get("type") == "json_object" else None,
        )

    def complete(self, *, model: str, messages: list[dict], temperature: Optional[float] = None,
                 response_format: Optional[dict] = None, **_):
        system, user = _to_gemini(messages)
        r = self._client.models.generate_content(model=model, contents=user,
                                                 config=self._config(system, temperature, response_format))
        return _fake_response(r.text or "")

    async def complete_async(self, *, model: str, messages: list[dict], temperature: Optional[float] = None,
                             response_format: Optional[dict] = None, **_):
        system, user = _to_gemini(messages)
        r = await self._client.aio.models.generate_content(model=model, contents=user,
                                                           config=self._config(system, temperature, response_format))
        return _fake_response(r.text or "")


# ---------------------------------------------------------------------------
# 即時辨識：Gemini Live → 產生 Mistral 的事件物件
# ---------------------------------------------------------------------------
class _Realtime:
    def __init__(self, client, language_code: str = ""):
        self._client = client
        self.language_code = language_code          # BCP-47，例如 "en-US"；空的 = 自動判斷

    async def transcribe_stream(self, *, audio_stream: AsyncIterator[bytes], model: str,
                                audio_format=None, target_streaming_delay_ms=None, **_):
        from google.genai import types
        from mistralai.client.models import (RealtimeTranscriptionError, RealtimeTranscriptionSessionCreated,
                                             TranscriptionStreamDone, TranscriptionStreamTextDelta)
        sample_rate = getattr(audio_format, "sample_rate", None) or 16000
        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=[self.language_code] if self.language_code else []),
        )
        texts: list[str] = []
        async with self._client.aio.live.connect(model=model, config=config) as session:
            yield RealtimeTranscriptionSessionCreated.model_validate({
                "type": "session.created",
                "session": {"request_id": f"gemini-{int(time.time() * 1000)}", "model": model,
                            "audio_format": {"encoding": "pcm_s16le", "sample_rate": sample_rate}}})
            sent_all = asyncio.Event()

            async def sender():
                # 把聲音一格一格送出去；到 9.5 分鐘（或使用者按停止、聲音送完）就告訴伺服器聲音結束
                started = time.monotonic()
                try:
                    async for chunk in audio_stream:
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={sample_rate}"))
                        if time.monotonic() - started > LIVE_SESSION_SECONDS:
                            break
                    await session.send_realtime_input(audio_stream_end=True)
                finally:
                    sent_all.set()

            send_task = asyncio.create_task(sender())
            deadline: Optional[float] = None       # 送出「聲音結束」後，最晚等到這個時間
            next_task: Optional[asyncio.Future] = None
            emitted = 0                            # 這一段話裡已經送出幾個字
            try:
                while True:
                    # session.receive() 每一輪對話結束就停，所以外面要再包一層迴圈
                    receiver = session.receive().__aiter__()
                    while True:
                        # 同時等「下一筆結果」和「聲音送完」，不去中斷 SDK 正在讀取的動作
                        if next_task is None:
                            next_task = asyncio.ensure_future(receiver.__anext__())
                        if not sent_all.is_set():
                            waiter = asyncio.ensure_future(sent_all.wait())
                            done, _ = await asyncio.wait({next_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                            if next_task not in done:
                                continue                   # 聲音送完了：下一圈開始倒數
                            waiter.cancel()
                        else:
                            deadline = deadline or time.monotonic() + LIVE_FINAL_WAIT
                            done, _ = await asyncio.wait({next_task}, timeout=max(0.0, deadline - time.monotonic()))
                            if not done:
                                raise _Finished()          # 等夠久了，最後的結果不會再來
                        task, next_task = next_task, None
                        try:
                            resp = task.result()
                        except StopAsyncIteration:
                            if sent_all.is_set():
                                raise _Finished()          # 聲音送完、這一輪結果也收完了
                            break                          # 還在送聲音：開下一輪接收
                        sc = getattr(resp, "server_content", None)
                        interim = getattr(sc, "interim_input_transcription", None) if sc else None
                        final = getattr(sc, "input_transcription", None) if sc else None
                        # 加速：確定結果要等一段話講完（連續講課時常常 15 秒以上），
                        # 暫時結果裡「已經講完、後面還接著新字」的句子幾乎不會再變，先送出去
                        if interim is not None and (interim.text or "").strip():
                            words = interim.text.split()
                            done = [i for i, w in enumerate(words[:-1]) if re.search(r"[.!?…]['\")\]]*$", w)]
                            if done and done[-1] + 1 > emitted:
                                piece = " ".join(words[emitted:done[-1] + 1])
                                emitted = done[-1] + 1
                                texts.append(piece)
                                yield TranscriptionStreamTextDelta(text=piece + " ")
                        if final is not None and (final.text or "").strip():
                            # 確定結果：只補上還沒送出的部分（用字數對齊，避免跟上面先送的重複）
                            rest = " ".join(final.text.split()[emitted:])
                            emitted = 0                        # 下一段話重新算
                            if rest:
                                texts.append(rest)
                                yield TranscriptionStreamTextDelta(text=rest + " ")
                    if send_task.done() and not send_task.cancelled() and send_task.exception():
                        raise send_task.exception()
            except _Finished:
                pass
            except Exception as e:
                yield RealtimeTranscriptionError.model_validate(
                    {"type": "error", "error": {"message": f"gemini live error: {e}", "code": 0}})
                return
            finally:
                if next_task is not None and not next_task.done():
                    next_task.cancel()
                if not send_task.done():
                    send_task.cancel()
        # 正常結束（主動換連線或按停止）：跟 Mistral 一樣送 transcription.done，外面的迴圈會自己重連
        yield TranscriptionStreamDone.model_validate({"type": "transcription.done", "model": model,
                                                      "text": " ".join(texts), "usage": {}, "language": None})


class _Finished(Exception):
    """送完聲音、也等完最後的辨識結果了。"""


# ---------------------------------------------------------------------------
# 整段辨識：Gemini 3.5 Transcribe → 模仿 Mistral 的 transcriptions.complete 回傳（.text, .segments）
# ---------------------------------------------------------------------------
def _seconds(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"([\d.]+)s?$", str(v or "0").strip())
    return float(m.group(1)) if m else 0.0


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _words(interaction) -> list[tuple[float, str]]:
    """從回應裡取出每個字和它的開始時間（steps[].content[].annotations[] 裡 type == word_info 的）。"""
    out = []
    for step in _get(interaction, "steps", None) or []:
        for content in _get(step, "content", None) or []:
            for ann in _get(content, "annotations", None) or []:
                if _get(ann, "type") == "word_info" and (_get(ann, "text") or "").strip():
                    out.append((_seconds(_get(ann, "start_offset")), _get(ann, "text").strip()))
    return out


def _segments_from_words(words: list[tuple[float, str]]) -> list:
    """把字依停頓（超過 0.8 秒）或句尾標點分成小段，做出跟 Mistral 一樣的 segments（start, text）。"""
    segs, cur, start, last = [], [], 0.0, 0.0
    for t, w in words:
        if cur and t - last > 0.8:
            segs.append(pytypes.SimpleNamespace(start=start, end=last, text=" ".join(cur)))
            cur = []
        if not cur:
            start = t
        cur.append(w)
        last = t
        if re.search(r"[.!?…]$", w) and len(cur) >= 8:
            segs.append(pytypes.SimpleNamespace(start=start, end=t, text=" ".join(cur)))
            cur = []
    if cur:
        segs.append(pytypes.SimpleNamespace(start=start, end=last, text=" ".join(cur)))
    return segs


class _Transcriptions:
    def __init__(self, client):
        self._client = client

    def complete(self, *, model: str, file: dict, language: Optional[str] = None, **_):
        data = file["content"]
        name = file.get("file_name", "part.flac")
        suffix = Path(name).suffix or ".flac"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:      # Files API 要檔案路徑
            tmp.write(data)
            tmp.flush()
            uploaded = self._client.files.upload(file=tmp.name, config={"mime_type": "audio/flac"})
        try:
            tc = {"mode": {"type": "verbatim", "timestamp_granularities": ["word"]}}
            if language:
                tc["language_codes"] = [BCP47.get(language, language)]
            interaction = self._client.interactions.create(
                model=model,
                input=[{"type": "audio", "uri": uploaded.uri, "mime_type": "audio/flac"}],
                generation_config={"transcription_config": tc})
        finally:
            try:
                self._client.files.delete(name=uploaded.name)
            except Exception:
                pass
        text = _get(interaction, "output_text", "") or ""
        segments = _segments_from_words(_words(interaction))
        return pytypes.SimpleNamespace(text=text, segments=segments, language=language)


# ---------------------------------------------------------------------------
class GeminiClient:
    """用法跟 mistralai.client.Mistral 一樣：GeminiClient(api_key)。"""

    def __init__(self, api_key: str, language: str = ""):
        from google import genai
        self._client = genai.Client(api_key=api_key)
        self.chat = _Chat(self._client)
        code = {"English": "en-US", "French": "fr-FR"}.get(language, "")
        self.audio = pytypes.SimpleNamespace(realtime=_Realtime(self._client, code),
                                             transcriptions=_Transcriptions(self._client))
