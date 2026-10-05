"""
測試你的 Mistral API Key 能不能用（翻譯用的 LLM + 即時語音辨識）
執行：python test_api.py
費用：極少（一個很短的對話 + 3 秒語音辨識）
"""
import asyncio
import os
from pathlib import Path

from mistralai.client import Mistral


def load_key() -> str:
    if os.getenv("MISTRAL_API_KEY"):
        return os.environ["MISTRAL_API_KEY"]
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MISTRAL_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 API Key：請先建立 .env 檔（參考 .env.example）")


def explain(err: Exception) -> str:
    s = str(err)
    if "401" in s:
        return "→ 金鑰無效（401）：請確認 .env 裡的金鑰有完整貼上"
    if "403" in s:
        return "→ 沒有權限（403）：你的帳號或金鑰不能使用這個功能，要問學校的 Mistral 管理員"
    if "429" in s:
        return "→ 請求太多或額度用完（429）"
    if "402" in s or "payment" in s.lower() or "credit" in s.lower() or "billing" in s.lower():
        return "→ 額度或付款問題：帳號可能沒有 API 額度，要問學校的 Mistral 管理員"
    return "→ 其他錯誤，請把上面整段訊息傳給 Claude"


client = Mistral(api_key=load_key())

print("【測試 1】翻譯用的 LLM（mistral-small-latest）")
llm_ok = False
try:
    r = client.chat.complete(model="mistral-small-latest",
                             messages=[{"role": "user", "content": "把 'Good morning' 翻成繁體中文，只回答翻譯"}])
    print("  ✅ 成功，回覆：", r.choices[0].message.content)
    llm_ok = True
except Exception as e:
    print("  ❌ 失敗：", e)
    print("  ", explain(e))


print("\n【測試 2】即時語音辨識（voxtral-mini-transcribe-realtime-2602）")


async def realtime_test():
    from mistralai.client.models import AudioFormat, RealtimeTranscriptionError

    async def silence():
        for _ in range(30):                     # 30 × 100 毫秒 = 3 秒靜音
            yield b"\x00\x00" * 1600
            await asyncio.sleep(0.1)

    types = []
    async for ev in client.audio.realtime.transcribe_stream(
            audio_stream=silence(), model="voxtral-mini-transcribe-realtime-2602",
            audio_format=AudioFormat(encoding="pcm_s16le", sample_rate=16000)):
        types.append(getattr(ev, "type", type(ev).__name__))
        if isinstance(ev, RealtimeTranscriptionError):
            raise RuntimeError(f"{ev.error.code} {ev.error.message}")
    return types


stt_ok = False
try:
    events = asyncio.run(realtime_test())
    print("  ✅ 成功，收到的事件：", events)
    stt_ok = True
except Exception as e:
    print("  ❌ 失敗：", e)
    print("  ", explain(e))

print("\n結論：", "兩項都能用，可以開始用 app.py 了 🎉" if llm_ok and stt_ok
      else "有項目失敗，把上面的訊息傳給 Claude")
