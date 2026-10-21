"""
研究用紀錄的自動化測試（不需要網路、不需要麥克風）

用假的 Mistral client（送音訊的方式模仿真正的 SDK：背景 task 逐段送、結束時取消）和產生的測試音訊，
檢查連線紀錄、ASR事件、健康指標、字幕時效，以及音訊序號能不能核對。

執行：.venv/bin/python -m unittest tests.test_research_logging -v
注意：這裡的「伺服器收到」是假 client 收到的；真正網路上伺服器有沒有收到，無法從程式觀測。
"""
from __future__ import annotations

import asyncio
import csv
import re
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from mistralai.client.models import (TranscriptionStreamDone, TranscriptionStreamLanguage,  # noqa: E402
                                     TranscriptionStreamSegmentDelta, TranscriptionStreamTextDelta)
from mistralai.extra.realtime.connection import UnknownRealtimeEvent  # noqa: E402
from websockets.exceptions import ConnectionClosedError  # noqa: E402
from websockets.frames import Close  # noqa: E402

SR = core.SAMPLE_RATE


def make_wav(path: Path, seconds: float, loud: bool = True):
    t = np.arange(int(SR * seconds)) / SR
    pcm = (0.3 * 32767 * np.sin(2 * np.pi * 220 * t)) if loud else np.zeros_like(t)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return int(np.ceil(len(t) / core.CHUNK_SAMPLES))          # 會切成幾段


def done_event():
    return TranscriptionStreamDone(model="fake", text="", usage={"prompt_tokens": 0, "completion_tokens": 0,
                                                                  "total_tokens": 0}, language=None)


def session_created(n):
    return types.SimpleNamespace(type="session.created", session=types.SimpleNamespace(
        request_id=f"req-{n}", model="fake-realtime", target_streaming_delay_ms=480))


class FakeRealtime:
    """每條連線照 behaviors[i] 演出；送音訊的寫法和 mistralai/extra/realtime/transcription.py 一樣。"""

    def __init__(self, behaviors):
        self.behaviors = behaviors
        self.received: list[list[bytes]] = []     # 每條連線假伺服器收到的音訊段

    async def transcribe_stream(self, audio_stream, **kw):
        n = len(self.received)
        beh = self.behaviors[min(n, len(self.behaviors) - 1)]
        got: list[bytes] = []
        self.received.append(got)
        if beh == "connect_fail":
            await asyncio.sleep(0.05)
            raise RuntimeError("Failed to connect: fake handshake failure")
        closed = {"v": False}

        async def _send():
            async for chunk in audio_stream:
                if closed["v"]:
                    break
                await asyncio.sleep(0)            # 模擬 websocket.send
                got.append(chunk)

        send_task = asyncio.create_task(_send())
        started = time.monotonic()
        try:
            yield session_created(n)
            if beh == "events":
                yield TranscriptionStreamLanguage(audio_language="en")
                yield TranscriptionStreamSegmentDelta(text="hello world", start=0.5, end=1.2)
                yield UnknownRealtimeEvent(type="transcription.mystery", content={"x": 1})
            while True:
                if send_task.done():               # 使用者按停止 → 音訊送完 → 伺服器回 done
                    yield done_event()
                    return
                await asyncio.sleep(0.25)
                elapsed = time.monotonic() - started
                if beh.startswith("1012@") and elapsed > float(beh[5:]):
                    raise ConnectionClosedError(Close(1012, "service restart"), Close(1012, "service restart"), True)
                if beh.startswith("end@") and elapsed > float(beh[4:]):
                    return                         # 伺服器自己結束串流（沒有 done、沒有錯誤）
                if beh.startswith("silent"):
                    continue
                yield TranscriptionStreamTextDelta(text=f"Word{len(got)} ")
        finally:
            closed["v"] = True
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass


class FakeChat:
    async def complete_async(self, **kw):
        await asyncio.sleep(0.01)
        msg = types.SimpleNamespace(content='{"translation": "中文", "terms": []}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class FastStallSession(core.LiveSession):
    STALL_SECONDS = 2               # 只在測試裡縮短；正式程式仍是 30 秒
    STALL_LOUD_CHUNKS = 10


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def end_audit(rows: list[dict]) -> dict:
    end = [r for r in rows if r["event"] == "end"][-1]
    return {k: int(v) for k, v in re.findall(r"(\w+_chunks|input_overflows)=(\d+)", end["detail"])}


class Run:
    """跑一次 LiveSession（音訊檔模擬麥克風），結束後提供各 CSV 內容。"""

    def __init__(self, behaviors, seconds=6.0, loud=True, speed=2.0, session_cls=core.LiveSession,
                 during=None, stop_after=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / "records" / "2026-01-01_0000_test"
        wav = root / "in.wav"
        self.expected_chunks = make_wav(wav, seconds, loud)
        self.expected_samples = int(SR * seconds)
        self.fake = FakeRealtime(behaviors)
        client = types.SimpleNamespace(audio=types.SimpleNamespace(realtime=self.fake), chat=FakeChat())
        cfg = core.SessionConfig(api_key="x", out_dir=self.out, course="test", wav_path=str(wav),
                                 wav_speed=speed, save_audio=True)
        self.s = session_cls(cfg, client_factory=lambda key: client)
        self.s._keep_awake = lambda: None           # 測試時不啟動 caffeinate
        self.s.start()
        if during:
            during(self.s)
        if stop_after:
            time.sleep(stop_after)
            self.s.stop()
        self.s.join(120)
        assert not self.s.running, "session did not finish"
        self.conn = read_csv(self.out / core.CONN_LOG)
        self.health = read_csv(self.out / core.HEALTH_LOG)
        p = self.out / core.LATENCY_LOG
        self.latency = read_csv(p) if p.exists() else []
        p = self.out / core.ASR_EVENT_LOG
        self.asr = read_csv(p) if p.exists() else []
        self.audit = end_audit(self.conn)

    def events(self, name):
        return [r for r in self.conn if r["event"] == name]

    def close(self):
        self.tmp.cleanup()


class ResearchLoggingTests(unittest.TestCase):
    def check_audio_accounting(self, run: Run):
        """收音段數 = 佇列剩下 + 交給 SDK + orphaned；假伺服器收到的介於 send_returned 和 handed 之間。"""
        a = run.audit
        self.assertEqual(a["captured_chunks"], run.expected_chunks, "every injected chunk was captured")
        queued_left = a["captured_chunks"] - a["dequeued_chunks"]
        self.assertEqual(a["captured_chunks"], queued_left + a["handed_chunks"] + a["orphaned_chunks"])
        received = sum(len(r) for r in run.fake.received)
        self.assertLessEqual(a["send_returned_chunks"], received)
        self.assertLessEqual(received, a["handed_chunks"])
        # 每條連線交給 SDK 的序號是連續區間；區間總長 = handed（扣掉落在區間裡的 orphaned）
        last_rows = {}
        for r in run.conn:
            if r["conn_first_handed_seq"] != "" and r["event"] in ("lost", "end", "stall_detected"):
                last_rows[r["conn_id"]] = r
        spans = sorted((int(r["conn_first_handed_seq"]), int(r["conn_last_handed_seq"])) for r in last_rows.values())
        for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
            self.assertGreater(a2, b1, "connections never hand the same chunk twice")
        self.assertEqual(sum(b - a + 1 for a, b in spans) - self._orphans_inside(run, spans), a["handed_chunks"])
        # FLAC 和收音完全一致
        import soundfile as sf
        flac_frames = sf.info(str(run.out / "錄音.flac")).frames
        self.assertEqual(flac_frames, run.expected_samples, "FLAC has every injected sample")
        self.assertAlmostEqual(flac_frames / SR, float(run.conn[-1]["audio_captured_seconds"]), places=1)

    @staticmethod
    def _orphans_inside(run, spans):
        seqs = [int(x) for x in re.findall(r"\d+", re.search(r"orphaned_seqs=\[([^\]]*)\]",
                                                              run.events("end")[-1]["detail"]).group(1))]
        return sum(1 for s in seqs if any(a <= s <= b for a, b in spans))

    def check_monotonic(self, run: Run):
        for rows in (run.conn, run.health, run.latency, run.asr):
            ts = [float(r["t_seconds"]) for r in rows]
            self.assertEqual(ts, sorted(ts), "t_seconds never goes backwards")

    # 1. 正常串流
    def test_01_normal_stream(self):
        run = Run(["text"], seconds=6)
        try:
            self.assertEqual(run.events("lost"), [])
            self.assertEqual(run.events("stall_detected"), [])
            self.assertGreater(len(run.latency), 5)
            self.assertTrue(all(r["handed_seq"] != "" for r in run.latency))
            self.assertEqual(run.audit["orphaned_chunks"], 0)
            self.assertEqual(run.audit["captured_chunks"], run.audit["dequeued_chunks"])
            self.assertEqual([r["event_type"] for r in run.asr], ["session.created", "transcription.done"])
            self.check_audio_accounting(run)
            self.check_monotonic(run)
        finally:
            run.close()

    # 2. 長時間靜音：不會誤判卡死
    def test_02_long_silence_no_stall(self):
        run = Run(["silent"], seconds=10, loud=False, session_cls=FastStallSession)
        try:
            self.assertEqual(run.events("stall_detected"), [])
            self.assertEqual(run.events("lost"), [])
            self.assertTrue(all(r["loud_chunks"] == "0" for r in run.health))
            self.check_audio_accounting(run)
        finally:
            run.close()

    # 3. Silent stall 觸發與恢復；8. 取消時的競態（真實流程中有沒有發生、有沒有被記到）
    def test_03_silent_stall_and_recovery(self):
        run = Run(["silent", "text"], seconds=14, session_cls=FastStallSession)
        try:
            names = [r["event"] for r in run.conn]
            i = names.index("stall_detected")
            self.assertEqual(names[i:i + 5], ["stall_detected", "lost", "reconnect_start", "session_created",
                                              "recovered"])
            sd, lost = run.conn[i], run.conn[i + 1]
            self.assertEqual(lost["error_type"], "SilentStall")
            self.assertGreaterEqual(int(sd["loud_chunks_since_text"]), FastStallSession.STALL_LOUD_CHUNKS)
            self.assertGreaterEqual(float(lost["t_seconds"]), float(sd["t_seconds"]))
            # 取消競態：每次拆掉連線最多被背景執行緒多拿走 1 段（100 毫秒），而且都有記到
            teardowns = len(run.events("lost"))
            self.assertLessEqual(run.audit["orphaned_chunks"], teardowns)
            # 卡住那條連線把音訊一直交給 SDK（這些音訊不會有字幕）
            self.assertGreater(int(lost["conn_last_handed_seq"]), int(lost["conn_first_handed_seq"]) + 10)
            self.check_audio_accounting(run)
            self.check_monotonic(run)
        finally:
            run.close()

    # 4. WebSocket 1012；7. 重連期間音訊積壓並補送
    def test_04_1012_and_backlog(self):
        run = Run(["1012@1.5", "text"], seconds=10)
        try:
            lost = run.events("lost")[0]
            self.assertEqual(lost["error_type"], "ConnectionClosedError:1012")
            self.assertEqual(lost["category"], "server restart (1012)")
            rs = run.events("reconnect_start")[0]
            self.assertGreater(float(rs["since_lost_seconds"]), 1.5)            # 退避約 2 秒
            # 退避期間有積壓
            down_backlog = [float(r["backlog_seconds"]) for r in run.health if r["down"] == "1"]
            self.assertGreater(max(down_backlog), 1.0)
            # 新連線第一段 = 斷線前最後交出的下一段（FIFO 補送；中間只可能少掉 orphaned）
            orphan_gap = int(rs["dequeued_seq"] or -1) - int(lost["handed_seq"])
            first_new = [r for r in run.conn if r["conn_id"] == rs["conn_id"] and r["conn_first_handed_seq"] != ""]
            self.assertTrue(first_new)
            self.assertEqual(int(first_new[0]["conn_first_handed_seq"]),
                             int(lost["conn_last_handed_seq"]) + 1 + max(orphan_gap, 0))
            self.check_audio_accounting(run)
        finally:
            run.close()

    # 5. 伺服器正常關閉（沒有 done、沒有錯誤）
    def test_05_server_normal_close(self):
        run = Run(["end@1.5", "text"], seconds=8)
        try:
            lost = run.events("lost")[0]
            self.assertEqual(lost["error_type"], "StreamEnded")
            self.assertEqual(lost["category"], "server ended stream (no error)")
            self.assertEqual(lost["attempt"], "")
            rs = run.events("reconnect_start")[0]
            self.assertLess(float(rs["since_lost_seconds"]), 0.5)              # 原本就不等待，行為不變
            self.assertTrue(run.events("recovered"))
            self.check_audio_accounting(run)
        finally:
            run.close()

    # 6. 連續重連失敗
    def test_06_repeated_reconnect_failures(self):
        run = Run(["1012@1", "connect_fail", "connect_fail", "text"], seconds=36, speed=2.0)
        try:
            lost = run.events("lost")
            self.assertEqual([r["attempt"] for r in lost[:3]], ["1", "2", "3"])
            waits = [float(r["since_lost_seconds"]) for r in run.events("reconnect_start")]
            # 退避 2、4、6 秒（since_lost 是從第一次宣告斷線起算的累計）
            self.assertTrue(1.8 < waits[0] < 3, waits)
            self.assertTrue(waits[1] - waits[0] > 3.5 and waits[2] - waits[1] > 5.5, waits)
            self.assertEqual(len(run.events("session_created")), 1)
            self.assertTrue(run.events("recovered"))
            self.assertEqual(len({r["conn_id"] for r in run.conn}), 5)           # 0 + 4 條連線
            self.check_audio_accounting(run)
        finally:
            run.close()

    # 8. 取消音訊 generator 時的競態（直接重現）
    def test_08_cancel_race_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = core.LiveSession(core.SessionConfig(api_key="x", out_dir=Path(tmp) / "c", save_audio=False))

            async def scenario(put_after_cancel: bool):
                stats = {"first": None, "last": None, "send_returned": None}
                gen = s._audio_stream(stats)
                task = asyncio.ensure_future(gen.__anext__())
                await asyncio.sleep(0.05)                 # 背景執行緒正在 get() 等音訊
                task.cancel()
                if put_after_cancel:
                    s._emit_audio(b"\x01\x00" * core.CHUNK_SAMPLES)   # 取消之後音訊才到
                await asyncio.sleep(0.4)
                return task

            asyncio.run(scenario(put_after_cancel=False))
            self.assertEqual(s._orphaned_seqs, [])
            asyncio.run(scenario(put_after_cancel=True))
            self.assertEqual(s._orphaned_seqs, [0], "chunk taken by the background thread is recorded")
            self.assertEqual((s._dequeued_count, s._handed_count), (1, 0))
            self.assertEqual(s._audio_q.qsize(), 0)

    # 9. 音訊序號連續（多次重連）
    def test_09_chunk_sequence_continuity(self):
        run = Run(["1012@1", "end@1", "silent", "text"], seconds=20, session_cls=FastStallSession)
        try:
            self.check_audio_accounting(run)
            seqs = [int(r["captured_seq"]) for r in run.health if r["captured_seq"] != ""]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(int(run.conn[-1]["captured_seq"]), run.expected_chunks - 1)
        finally:
            run.close()

    # 10. ASR segment、未知事件與其他非 delta 事件
    def test_10_non_delta_events(self):
        run = Run(["events"], seconds=5)
        try:
            types_ = [r["event_type"] for r in run.asr]
            self.assertEqual(types_[:4], ["session.created", "transcription.language", "transcription.segment",
                                          "transcription.mystery"])
            self.assertEqual(types_[-1], "transcription.done")
            self.assertNotIn("transcription.text.delta", types_)               # delta 只在 subtitle_latency.csv
            seg = run.asr[2]
            self.assertEqual((seg["seg_start_raw"], seg["seg_end_raw"]), ("0.5", "1.2"))
            self.assertIn("request_id=req-0", run.asr[0]["detail"])
            self.assertIn("audio_language=en", run.asr[1]["detail"])
            self.assertIn("content=", run.asr[3]["detail"])
            self.assertTrue(all(r["class_folder"] == run.out.name and r["conn_id"] == "1" for r in run.asr))
            self.assertEqual(run.events("lost"), [])                             # 未知事件不會造成斷線
        finally:
            run.close()

    # 11. 麥克風 input overflow
    def test_11_input_overflow(self):
        raw_q = __import__("queue").Queue()

        def inject(s):
            time.sleep(1.0)
            st = types.SimpleNamespace(input_overflow=True)
            for _ in range(3):
                s._mic_callback(raw_q, b"\x00\x00" * core.CHUNK_SAMPLES, st)
            s._mic_callback(raw_q, b"\x00\x00" * core.CHUNK_SAMPLES, types.SimpleNamespace(input_overflow=False))

        run = Run(["text"], seconds=5, during=inject)
        try:
            self.assertEqual(raw_q.qsize(), 4)                                   # callback 照樣把音訊放進佇列
            self.assertEqual(sum(int(r["input_overflows"]) for r in run.health), 3)
            self.assertEqual(run.audit["input_overflows"], 3)
        finally:
            run.close()

    # 寫檔失敗要看得到
    def test_12_log_write_failure_is_visible(self):
        errors = []
        buf = core._CsvBuffer(Path("/nonexistent-dir/x.csv"), ["a"], errors.append)
        buf.add({"a": 1})
        buf.flush()
        self.assertEqual(buf.failures, 1)
        self.assertTrue(errors and "could not be written" in errors[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
