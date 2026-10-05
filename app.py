"""
即時上課翻譯＋筆記（Streamlit 介面）

執行：
    streamlit run app.py
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import streamlit as st

from core import (DEFAULT_NOTES_MODEL, DEFAULT_STT_MODEL, DEFAULT_TRANSLATE_MODEL,
                  LiveSession, SessionConfig, fmt_time, generate_notes, list_input_devices)

APP_DIR = Path(__file__).parent
RECORDS_DIR = APP_DIR / "records"
GLOSSARY_DIR = APP_DIR / "詞彙表"


def load_env_key() -> str:
    """優先讀環境變數 MISTRAL_API_KEY，其次讀同資料夾的 .env 檔。"""
    if os.getenv("MISTRAL_API_KEY"):
        return os.environ["MISTRAL_API_KEY"]
    env = APP_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MISTRAL_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "未命名課程"


def load_glossary(course: str) -> str:
    f = GLOSSARY_DIR / f"{safe_name(course)}.txt"
    return f.read_text(encoding="utf-8") if f.exists() else ""


st.set_page_config(page_title="即時上課翻譯", page_icon="🎧", layout="wide")


class SharedState:
    """整個程式共用一份（不跟著瀏覽器分頁走）：重新整理網頁後，還能接回正在進行的錄音。"""
    session: LiveSession | None = None
    notes: str | None = None
    notes_path: Path | None = None


@st.cache_resource
def get_shared_state() -> SharedState:
    return SharedState()


ss = get_shared_state()

# ---------------------------------------------------------------- 側邊欄
with st.sidebar:
    st.header("設定")
    # 金鑰只從 .env（或環境變數）讀取，留在本機程式裡，不送到瀏覽器
    api_key = load_env_key()
    if not api_key:
        st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
    course = st.text_input("課程名稱", placeholder="例如：Strategies in the arts")

    # 每門課一份詞彙表，存在 詞彙表/課程名稱.txt，下次填同一個課程名稱會自動載入
    glossary_file = GLOSSARY_DIR / f"{safe_name(course)}.txt"
    saved_glossary = load_glossary(course)
    glossary = st.text_area("課程詞彙表（選填）", value=saved_glossary, key=f"glossary_{safe_name(course)}",
                            placeholder="每行一個：原文 = 中文\nmerit goods = 有益財\narm's length principle = 臂距原則",
                            help="翻譯和筆記遇到這些名詞，會固定用你指定的中文。依課程名稱分開自動存檔。")
    if glossary != saved_glossary:
        GLOSSARY_DIR.mkdir(exist_ok=True)
        glossary_file.write_text(glossary, encoding="utf-8")

    source = st.radio("音訊來源", ["麥克風", "音訊檔（模擬上課）"], horizontal=True)
    device, wav_path, wav_speed = None, None, 1.0
    if source == "麥克風":
        try:
            devices = list_input_devices()
            names = ["系統預設麥克風"] + [f"{i}: {n}" for i, n in devices]
            pick = st.selectbox("麥克風", names)
            if pick != names[0]:
                device = int(pick.split(":")[0])
        except Exception as e:
            st.warning(f"讀不到麥克風清單：{e}")
    else:
        up = st.file_uploader("上傳 WAV 檔（16-bit）", type=["wav"])
        wav_speed = st.slider("播放速度", 1.0, 4.0, 1.0, 0.5,
                              help="1.0 = 跟真實上課一樣快；調快可以快速測試，但費用照音訊長度計算")
        if up is not None:
            tmp = APP_DIR / "records" / "_upload.wav"
            # 只有換了新檔案才寫入，避免每次操作介面都重寫一次
            if st.session_state.get("uploaded_id") != up.file_id or not tmp.exists():
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(up.getvalue())
                st.session_state.uploaded_id = up.file_id
            wav_path = str(tmp)

    with st.expander("進階設定"):
        stt_model = st.text_input("語音辨識模型", DEFAULT_STT_MODEL)
        trans_model = st.text_input("翻譯模型", DEFAULT_TRANSLATE_MODEL)
        notes_model = st.text_input("筆記模型", DEFAULT_NOTES_MODEL)
        delay_opt = st.selectbox("辨識延遲（毫秒）", ["預設", "240", "480", "960", "1600", "2400"],
                                 help="延遲越短字幕越快出現，延遲越長通常越準確")
        min_chars = st.slider("每段至少幾個字元才翻譯", 30, 200, 80, 10,
                              help="數字越大，翻譯請求越少、每段越長")
        newest_first = st.checkbox("最新的顯示在最上面", value=True)
        max_rows = st.slider("畫面最多顯示幾段", 10, 200, 40, 10)

# ---------------------------------------------------------------- 主畫面
st.title("🎧 即時上課翻譯＋筆記")
sess: LiveSession | None = ss.session
running = bool(sess and sess.running)

c1, c2, c3 = st.columns(3)
if c1.button("▶ 開始", type="primary", disabled=running, use_container_width=True):
    if not api_key:
        st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
    elif source != "麥克風" and not wav_path:
        st.error("請先上傳 WAV 檔")
    else:
        out_dir = RECORDS_DIR / f"{datetime.now():%Y-%m-%d_%H%M}_{safe_name(course)}"
        cfg = SessionConfig(api_key=api_key, out_dir=out_dir, course=course,
                            stt_model=stt_model, translate_model=trans_model,
                            target_delay_ms=None if delay_opt == "預設" else int(delay_opt),
                            input_device=device, wav_path=wav_path, wav_speed=wav_speed,
                            min_chars=min_chars, glossary=glossary)
        ss.session = LiveSession(cfg)
        ss.notes = ss.notes_path = None
        ss.session.start()
        st.rerun()

if c2.button("⏹ 停止", disabled=not running, use_container_width=True):
    sess.stop()
    st.rerun()

def make_notes(transcript: str, course: str, date_str: str, out_dir: Path, glossary: str):
    """產生筆記並存到 out_dir/筆記.md。分段結果先暫存，失敗後再按一次會接續。"""
    cache_dir = out_dir / ".筆記暫存"
    with st.status("產生筆記中…", expanded=True) as box:
        try:
            notes = generate_notes(api_key, transcript, course, date_str, model=notes_model,
                                   progress=box.write, cache_dir=cache_dir, glossary=glossary)
            path = out_dir / "筆記.md"
            path.write_text(notes, encoding="utf-8")
            shutil.rmtree(cache_dir, ignore_errors=True)
            ss.notes, ss.notes_path = notes, path
            box.update(label="筆記完成", state="complete")
        except Exception as e:
            box.update(label=f"產生筆記失敗：{e}（已完成的段落有暫存，再按一次會接續）", state="error")


can_note = bool(sess and not sess.running and sess.segments)
if c3.button("📝 產生筆記", disabled=not can_note, use_container_width=True):
    if not api_key:
        st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
    else:
        make_notes(sess.full_source_text(), sess.cfg.course,
                   f"{datetime.fromtimestamp(sess.started_at):%Y-%m-%d}", sess.cfg.out_dir, sess.cfg.glossary)

# 程式當掉、關掉後，也能用 records/ 裡存好的逐字稿產生筆記
saved = sorted((d for d in RECORDS_DIR.glob("*/") if d.is_dir()
                and (d / "逐字稿_原文.txt").exists() and (d / "逐字稿_原文.txt").stat().st_size > 0),
               reverse=True) if RECORDS_DIR.exists() else []
with st.expander("📂 從之前的逐字稿產生筆記"):
    if not saved:
        st.caption("records/ 裡還沒有逐字稿")
    else:
        pick = st.selectbox("選擇課堂", saved,
                            format_func=lambda d: d.name + ("（已有筆記）" if (d / "筆記.md").exists() else ""))
        if st.button("📝 用這份逐字稿產生筆記", disabled=running and sess.cfg.out_dir == pick):
            if not api_key:
                st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
            else:
                # 資料夾名稱格式：日期_時間_課程名稱
                date_str, _, rest = pick.name.partition("_")
                course_name = rest.partition("_")[2]
                make_notes((pick / "逐字稿_原文.txt").read_text(encoding="utf-8"),
                           course_name, date_str, pick, load_glossary(course_name))


@st.fragment(run_every=1.0)
def live_view():
    s: LiveSession | None = ss.session
    if s is None:
        st.info("按「▶ 開始」開始錄音。第一次使用前，請先看 README.md。")
        return
    # 錄音結束（按停止或檔案播完）→ 重新整理整頁，讓按鈕狀態更新
    if st.session_state.get("was_running") and not s.running:
        st.session_state.was_running = False
        st.rerun()
    st.session_state.was_running = s.running
    segs, partial, status, errors, lang = s.snapshot()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("狀態", status)
    m2.metric("錄音時間", fmt_time(s.elapsed()))
    m3.metric("偵測語言", lang or "—")
    m4.metric("辨識費用估計", f"${s.est_cost():.2f}")
    if errors:
        with st.expander(f"⚠️ 訊息（{len(errors)}）"):
            st.text("\n".join(errors[-20:]))

    rows = segs[-max_rows:]
    if newest_first:
        rows = rows[::-1]
    if partial and s.running:
        st.caption(f"辨識中：{partial}")
    for seg in rows:
        left, right = st.columns(2)
        left.markdown(f"`{fmt_time(seg.elapsed)}` {seg.source}")
        if seg.translation is None:
            right.markdown("_翻譯中…_")
        elif seg.error:
            right.markdown(f":red[{seg.error}]")
        else:
            right.markdown(seg.translation)
    if not s.running and segs:
        st.success(f"已存檔：{s.transcript_path}")


live_view()

if ss.notes:
    st.divider()
    st.subheader("📝 筆記")
    st.caption(f"已存檔：{ss.notes_path}")
    st.download_button("下載筆記（.md）", ss.notes, file_name=Path(ss.notes_path).name)
    st.markdown(ss.notes)
