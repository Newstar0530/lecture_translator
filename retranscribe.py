"""
下課後從錄音檔重新辨識

即時辨識斷線、卡住、聽錯的部分，下課後用 Mistral 的「整段錄音辨識」（voxtral-mini）重新辨識一次：
看得到完整上下文、可以指定語言、可以給專有名詞提示，通常比即時辨識準。
原本的即時逐字稿保留不動，結果另外存成「transcript_source_full.txt」「transcript_bilingual_full.md」。
"""
from __future__ import annotations

import csv
import io
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from core import (CONFIG_FILE, DEFAULT_TRANSLATE_MODEL, _message_text, build_translate_messages, fmt_time,
                  merge_terms, parse_translation, terms_in)

BATCH_STT_MODEL = "voxtral-mini-2602"   # 固定版本（＝ 2026-10 的 voxtral-mini-latest），參考時間軸才不會中途換模型
PART_SECONDS = 600            # 每段 10 分鐘
CUT_SEARCH_SECONDS = 30       # 在每段結尾前 30 秒內找最安靜的地方切
LANG_CODES = {"English": "en", "French": "fr"}
FULL_SOURCE = "transcript_source_full.txt"
FULL_BILINGUAL = "transcript_bilingual_full.md"
REFERENCE_TIMELINE = "reference_timeline.csv"   # 每個字在 recording.flac 裡的開始、結束秒數（算有效字幕可用率的基準）

Line = tuple[float, str]      # (開始秒數, 原文)
Word = tuple[float, float, str]   # (開始秒數, 結束秒數, 文字；英文字前面通常帶空格)


def split_audio(path: Path, part_seconds: int = PART_SECONDS) -> list[tuple[float, bytes]]:
    """把錄音切成約 10 分鐘一段（在安靜的地方切，避免切到句子中間），回傳 [(開始秒數, FLAC 位元組)]。"""
    import soundfile as sf
    audio, sr = sf.read(str(path), dtype="int16")
    if audio.ndim > 1:
        audio = audio[:, 0]
    win = sr // 10                                   # 100 毫秒一格
    parts, start = [], 0
    while start < len(audio):
        end = start + part_seconds * sr
        if end >= len(audio):
            end = len(audio)
        else:
            # 在 end 前 30 秒內，找音量最小的 100 毫秒當切點
            lo = max(start + sr, end - CUT_SEARCH_SECONDS * sr)
            seg = audio[lo:end].astype(np.float32)
            n = len(seg) // win
            if n > 0:
                rms = np.sqrt((seg[: n * win].reshape(n, win) ** 2).mean(axis=1))
                end = lo + int(np.argmin(rms)) * win + win // 2
        buf = io.BytesIO()
        sf.write(buf, audio[start:end], sr, format="FLAC", subtype="PCM_16")
        parts.append((start / sr, buf.getvalue()))
        start = end
    return parts


def _transcribe_part(client, data: bytes, language: str, bias: list[str]) -> list[Word]:
    """辨識一段錄音，回傳每個字 [(開始秒數, 結束秒數, 文字)]（這段裡的秒數）。
    失敗會重試；專有名詞提示被拒絕就拿掉再試。"""
    kw = {"model": BATCH_STT_MODEL, "file": {"file_name": "part.flac", "content": data},
          "timestamp_granularities": ["word"]}
    if LANG_CODES.get(language):
        kw["language"] = LANG_CODES[language]
    if bias:
        kw["context_bias"] = bias
    for attempt in range(4):
        try:
            r = client.audio.transcriptions.complete(**kw)
            words = [(float(s.start or 0), float(s.end or s.start or 0), s.text) for s in (r.segments or [])
                     if s.text.strip()]
            return words or ([(0.0, 0.0, r.text.strip())] if r.text.strip() else [])
        except Exception as e:
            msg = str(e)
            if "401" in msg or "403" in msg or attempt == 3:
                raise
            if "context_bias" in kw and ("422" in msg or "400" in msg):
                kw.pop("context_bias")               # 提示詞格式不被接受：拿掉再試
                continue
            time.sleep(10 * (attempt + 1) if "429" in msg else 3 * (attempt + 1))
    return []


def group_lines(words: list[Word]) -> list[Line]:
    """把辨識出的字合成適合閱讀和翻譯的句子：到句尾且夠長、或太長、或中間停頓很久就換一行。"""
    lines: list[Line] = []
    cur, cur_start, last_end = "", 0.0, 0.0
    for start, end, text in words:
        if cur and start - last_end > 4 and len(cur) >= 40:     # 停頓超過 4 秒
            lines.append((cur_start, cur.strip()))
            cur = ""
        if not cur:
            cur_start = start
            text = text.lstrip()
        # 英文字本身前面帶空格；沒帶空格的（例如分段開頭）補一個
        cur = cur + text if (not cur or text[:1].isspace()) else f"{cur} {text}"
        last_end = end
        if (len(cur) >= 90 and re.search(r"[.!?…]['\")\]]*$", cur)) or len(cur) >= 320:
            lines.append((cur_start, cur.strip()))
            cur = ""
    if cur.strip():
        lines.append((cur_start, cur.strip()))
    return lines


def write_reference_timeline(path: Path, words: list[Word]):
    """把每個字的時間存成 CSV：和即時字幕的 subtitle_latency.csv 對照，就能算每段話的字幕有沒有及時出現。"""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_seconds", "end_seconds", "text"])
        w.writerows((round(a, 2), round(b, 2), t.strip()) for a, b, t in words)


def _note_config(out_dir: Path, info: dict):
    """把課後重新辨識用的模型和參數記進這堂課的 config.json（沒有就新建）。"""
    import json
    path = out_dir / CONFIG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data["retranscribe"] = info
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _translate_line(client, lines: list[Line], i: int, glossary: dict[str, str], background: str,
                    language: str, model: str) -> str:
    context = " ".join(t for _, t in lines[max(0, i - 2):i])
    text = lines[i][1]
    msgs = build_translate_messages(context, text, terms_in(f"{context} {text}", glossary), background, language)
    for attempt in range(3):
        try:
            resp = client.chat.complete(model=model, messages=msgs, temperature=0.2,
                                        response_format={"type": "json_object"})
            return parse_translation(_message_text(resp))[0]
        except Exception as e:
            if attempt == 2:
                return f"（翻譯失敗：{e}）"
            time.sleep(5 * (attempt + 1) if "429" in str(e) else 1.5 * (attempt + 1))
    return ""


def retranscribe(api_key: str, out_dir: Path, course: str, language: str = "English",
                 slide_terms: Optional[dict[str, str]] = None, fixed_terms: Optional[dict[str, str]] = None,
                 background: str = "", translate_model: str = DEFAULT_TRANSLATE_MODEL,
                 progress: Optional[Callable[[str], None]] = None, client=None) -> list[Line]:
    """
    從 out_dir/recording.flac 重新辨識整堂課並翻譯，存成完整逐字稿，回傳 [(開始秒數, 原文)]。
    progress 只會在呼叫這個函式的執行緒裡被呼叫（Streamlit 可以直接拿來更新畫面）。
    """
    say = progress or (lambda s: None)
    audio = out_dir / "recording.flac"
    if not audio.exists():
        raise FileNotFoundError("這堂課沒有錄音檔（錄音時「Also save the recording」要打開）")
    if client is None:
        from mistralai.client import Mistral
        client = Mistral(api_key=api_key)
    glossary = merge_terms(slide_terms or {}, fixed_terms or {})
    # 專有名詞提示：簡報和修正過的原文名詞（只取多字的專有名詞，最多 100 個）
    bias = [en for en in glossary if " " in en.strip() or en[:1].isupper()][:100]

    say("Splitting the recording…")
    parts = split_audio(audio)
    say(f"Transcribing {len(parts)} part(s) of ~10 minutes…")
    results: dict[int, list[Word]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_transcribe_part, client, data, language, bias): i
                   for i, (_, data) in enumerate(parts)}
        for n, fut in enumerate(as_completed(futures), 1):
            i = futures[fut]
            offset = parts[i][0]
            results[i] = [(offset + a, offset + b, text) for a, b, text in fut.result()]
            say(f"Transcribed {n}/{len(parts)}")
    words = [w for i in sorted(results) for w in results[i]]
    write_reference_timeline(out_dir / REFERENCE_TIMELINE, words)
    _note_config(out_dir, {"model": BATCH_STT_MODEL, "timestamp_granularity": "word", "part_seconds": PART_SECONDS,
                           "language": language, "context_bias_terms": len(bias),
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    lines = group_lines(words)
    if not lines:
        raise ValueError("錄音裡沒有辨識出任何文字")

    say(f"Translating {len(lines)} lines…")
    translations = [""] * len(lines)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_translate_line, client, lines, i, glossary, background, language,
                               translate_model): i for i in range(len(lines))}
        for n, fut in enumerate(as_completed(futures), 1):
            translations[futures[fut]] = fut.result()
            if n % 25 == 0 or n == len(lines):
                say(f"Translated {n}/{len(lines)}")

    (out_dir / FULL_SOURCE).write_text("\n".join(f"[{fmt_time(t)}] {text}" for t, text in lines) + "\n",
                                       encoding="utf-8")
    md = [f"# {course or '上課逐字稿'}（從錄音重新辨識）\n"]
    md += [f"**[{fmt_time(t)}]** {text}\n\n> {zh or '（翻譯失敗）'}\n" for (t, text), zh in zip(lines, translations)]
    (out_dir / FULL_BILINGUAL).write_text("\n".join(md), encoding="utf-8")
    return lines
