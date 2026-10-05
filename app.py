"""
即時上課翻譯＋筆記（Streamlit 介面）

執行：
    streamlit run app.py
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import streamlit as st

from core import (DEFAULT_NOTES_MODEL, DEFAULT_STT_MODEL, DEFAULT_TRANSLATE_MODEL,
                  LiveSession, SessionConfig, TermBook, analyze_slides, extract_slide_text, fix_brief,
                  fmt_time, generate_notes, list_input_devices, merge_terms)

APP_DIR = Path(__file__).parent
RECORDS_DIR = APP_DIR / "records"
TERMS_DIR = APP_DIR / "專有名詞"


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


def load_fixed_terms(course: str) -> dict[str, str]:
    """讀取這門課修正過的譯名（專有名詞/課程名稱.json）。"""
    f = TERMS_DIR / f"{safe_name(course)}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


SLIDES_FILE = "簡報重點.json"


def load_slides_info(out_dir: Path) -> dict:
    """讀取這堂課開始錄音時存下的簡報重點（課程背景＋專有名詞）。"""
    f = out_dir / SLIDES_FILE
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def save_fixed_terms(course: str, terms: dict[str, str]):
    TERMS_DIR.mkdir(exist_ok=True)
    (TERMS_DIR / f"{safe_name(course)}.json").write_text(
        json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")


st.set_page_config(page_title="即時上課翻譯", page_icon="🎧", layout="wide")

# 字幕、狀態列的樣式（主題色在 .streamlit/config.toml）
st.html("""<style>
:root { --muted:#8b93a7; --line:#262b36; --card:#161a22; --accent:#6ea8fe; --rec:#ff5c5c; --warn:#f5b84b; }
.block-container { padding-top: 3.2rem; max-width: 1100px; }
.app-head { margin-bottom: .9rem; }
.app-title { font-size: 1.6rem; font-weight: 700; letter-spacing: .02em; margin: 0; }
.app-sub { color: var(--muted); font-size: .9rem; margin-top: .15rem; }
.statusbar { display:flex; flex-wrap:wrap; gap:.5rem 1.4rem; align-items:center; padding:.6rem .9rem;
  border:1px solid var(--line); border-radius:12px; background:var(--card); font-size:.92rem; margin-bottom: .9rem; }
.statusbar .k { color: var(--muted); margin-right:.35rem; }
.statusbar .v { font-variant-numeric: tabular-nums; }
.pill { display:inline-flex; align-items:center; gap:.45rem; font-weight:600; }
.dot { width:.6rem; height:.6rem; border-radius:50%; background:var(--muted); }
.pill.rec .dot { background:var(--rec); box-shadow:0 0 0 0 rgba(255,92,92,.6); animation:pulse 1.6s infinite; }
.pill.wait .dot { background:var(--warn); }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(255,92,92,.55)} 70%{box-shadow:0 0 0 .5rem rgba(255,92,92,0)} 100%{box-shadow:0 0 0 0 rgba(255,92,92,0)} }
.zh { font-family: "PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif; }
.listening { color: var(--muted); font-size:.92rem; padding:.2rem .1rem .8rem; }
.listening b { color: var(--accent); font-weight:600; margin-right:.4rem; }
.latest { border:1px solid #2f3b55; border-left:4px solid var(--accent); border-radius:12px;
  background:linear-gradient(180deg,#18202f,#151a24); padding:1rem 1.2rem; margin-bottom:1rem; }
.latest .zh { font-size:1.55rem; line-height:1.6; font-weight:500; }
.latest .en { color: var(--muted); font-size:.95rem; margin-top:.45rem; line-height:1.5; }
.seg { display:grid; grid-template-columns: 4.6rem 1fr; gap:.2rem .9rem; padding:.75rem .2rem; border-bottom:1px solid var(--line); }
.seg .t, .latest .t { color: var(--muted); font-size:.78rem; font-variant-numeric: tabular-nums; padding-top:.2rem; }
.latest .t { margin-bottom:.35rem; }
.seg .zh { font-size:1.06rem; line-height:1.65; }
.seg .en { grid-column:2; color: var(--muted); font-size:.86rem; line-height:1.5; }
.pending { color: var(--muted); font-style: italic; }
.err { color: var(--rec); }
.empty { border:1px dashed var(--line); border-radius:14px; padding:2rem 1.6rem; color: var(--muted); }
.empty h3 { color:#e6e8ee; margin:0 0 .8rem; font-size:1.15rem; }
.empty ol { margin:0; padding-left:1.2rem; line-height:2; }
.st-key-notes { padding: .6rem 1.2rem; }
.st-key-notes h1 { font-size:1.45rem; padding-top:.4rem; }
.st-key-notes h2 { font-size:1.15rem; padding-top:1rem; border-top:1px solid var(--line); margin-top:.6rem; }
.st-key-notes h3 { font-size:1.02rem; }
.st-key-notes hr { display:none; }
</style>""")


class SharedState:
    """整個程式共用一份（不跟著瀏覽器分頁走）：重新整理網頁後，還能接回正在進行的錄音。"""
    session: LiveSession | None = None
    notes: str | None = None
    notes_path: Path | None = None


@st.cache_resource
def get_shared_state() -> SharedState:
    return SharedState()


ss = get_shared_state()

# ---------------------------------------------------------------- 側邊欄：上課前的設定
with st.sidebar:
    # 金鑰只從 .env（或環境變數）讀取，留在本機程式裡，不送到瀏覽器
    api_key = load_env_key()
    if not api_key:
        st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")

    st.markdown("#### 這堂課")
    course = st.text_input("課程名稱", placeholder="例如：Strategies in the arts")

    # 上課前讀簡報：整理課程背景和專有名詞，翻譯和筆記都會用到。同一個檔案只分析一次
    slides_file = st.file_uploader("上課簡報（建議）", type=["pdf", "pptx"],
                                   help="上課前先上傳 PDF 或 PPTX，翻譯會依照這堂課的領域用詞，並採用簡報裡專有名詞的通用譯名")
    slides = None
    if slides_file is not None:
        data = slides_file.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        cached = st.session_state.get("slides")
        if cached and cached["digest"] == digest:
            slides = cached
        elif api_key:
            with st.spinner("讀取簡報中…（約 10 秒）"):
                try:
                    brief, terms = analyze_slides(api_key, extract_slide_text(slides_file.name, data))
                    slides = {"digest": digest, "name": slides_file.name, "summary": brief, "terms": terms}
                    st.session_state.slides = slides
                except Exception as e:
                    st.warning(f"讀不了這份簡報：{e}")
        if slides:
            with st.container(border=True):
                st.caption(f"📖 {slides['summary']}")
                with st.popover(f"簡報裡的 {len(slides['terms'])} 個專有名詞", use_container_width=True):
                    st.markdown("\n".join(f"- {en} → **{zh}**" for en, zh in slides["terms"].items()) or "（沒有）")

    st.markdown("#### 收音")
    source = st.segmented_control("音訊來源", ["麥克風", "音訊檔"], default="麥克風",
                                  label_visibility="collapsed") or "麥克風"
    device, wav_path, wav_speed = None, None, 1.0
    if source == "麥克風":
        try:
            devices = list_input_devices()
            names = ["系統預設麥克風"] + [f"{i}: {n}" for i, n in devices]
            pick = st.selectbox("麥克風", names, label_visibility="collapsed")
            if pick != names[0]:
                device = int(pick.split(":")[0])
        except Exception as e:
            st.warning(f"讀不到麥克風清單：{e}")
    else:
        up = st.file_uploader("WAV 檔（16-bit），模擬上課用", type=["wav"])
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
    save_audio = st.toggle("同時保存錄音檔（FLAC）", value=True,
                           help="存在這堂課的資料夾，可以之後重聽。每小時約 60–90 MB，不影響辨識和費用")

    terms_slot = st.container()   # 修正譯名（函式在檔案最後定義）

    with st.expander("進階設定"):
        stt_model = st.text_input("語音辨識模型", DEFAULT_STT_MODEL)
        trans_model = st.text_input("翻譯模型", DEFAULT_TRANSLATE_MODEL)
        notes_model = st.text_input("筆記模型", DEFAULT_NOTES_MODEL)
        delay_opt = st.selectbox("辨識延遲（毫秒）", ["預設", "240", "480", "960", "1600", "2400"],
                                 help="延遲越短字幕越快出現，延遲越長通常越準確")
        min_chars = st.slider("每段至少幾個字元才翻譯", 30, 200, 80, 10,
                              help="數字越小，字幕越快出現、翻譯請求越多")
        newest_first = st.checkbox("最新的顯示在最上面", value=True)
        max_rows = st.slider("畫面最多顯示幾段", 10, 200, 40, 10)

# ---------------------------------------------------------------- 主畫面
sess: LiveSession | None = ss.session
running = bool(sess and sess.running)

shown_course = (sess.cfg.course if sess else course) or "未命名課程"
st.html(f'<div class="app-head"><div class="app-title">🎧 即時上課翻譯</div>'
        f'<div class="app-sub">{html.escape(shown_course)}</div></div>')
b1, b2, b3 = st.columns([1, 1, 1.2], vertical_alignment="center")
start_clicked = stop_clicked = False
if running:
    stop_clicked = b1.button("⏹ 停止", use_container_width=True)
else:
    start_clicked = b1.button("● 開始錄音", type="primary", use_container_width=True)
can_note = bool(sess and not sess.running and sess.segments)
note_clicked = b2.button("📝 產生筆記", disabled=not can_note, use_container_width=True)
show_en = b3.toggle("顯示英文原文", value=True)

if start_clicked:
    if not api_key:
        st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
    elif source != "麥克風" and not wav_path:
        st.error("請先在左邊上傳 WAV 檔")
    else:
        out_dir = RECORDS_DIR / f"{datetime.now():%Y-%m-%d_%H%M}_{safe_name(course)}"
        cfg = SessionConfig(api_key=api_key, out_dir=out_dir, course=course,
                            stt_model=stt_model, translate_model=trans_model,
                            target_delay_ms=None if delay_opt == "預設" else int(delay_opt),
                            input_device=device, wav_path=wav_path, wav_speed=wav_speed,
                            min_chars=min_chars, fixed_terms=load_fixed_terms(course),
                            slide_brief=slides["summary"] if slides else "",
                            slide_terms=slides["terms"] if slides else {}, save_audio=save_audio)
        ss.session = LiveSession(cfg)
        if slides:   # 存一份，之後從舊逐字稿產生筆記時也能用
            (out_dir / SLIDES_FILE).write_text(json.dumps(
                {"name": slides["name"], "summary": slides["summary"], "terms": slides["terms"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
        ss.notes = ss.notes_path = None
        ss.session.start()
        st.rerun()

if stop_clicked:
    sess.stop()
    st.rerun()


def make_notes(transcript: str, course: str, date_str: str, out_dir: Path,
               glossary: dict[str, str], background: str):
    """產生筆記並存到 out_dir/筆記.md。分段結果先暫存，失敗後再按一次會接續。"""
    cache_dir = out_dir / ".筆記暫存"
    with st.status("產生筆記中…", expanded=True) as box:
        try:
            notes = generate_notes(api_key, transcript, course, date_str, model=notes_model,
                                   progress=box.write, cache_dir=cache_dir, glossary=glossary, background=background)
            path = out_dir / "筆記.md"
            path.write_text(notes, encoding="utf-8")
            shutil.rmtree(cache_dir, ignore_errors=True)
            ss.notes, ss.notes_path = notes, path
            box.update(label="筆記完成，在字幕下方", state="complete", expanded=False)
        except Exception as e:
            box.update(label=f"產生筆記失敗：{e}（已完成的段落有暫存，再按一次會接續）", state="error")


if note_clicked:
    if not api_key:
        st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
    else:
        make_notes(sess.full_source_text(), sess.cfg.course,
                   f"{datetime.fromtimestamp(sess.started_at):%Y-%m-%d}", sess.cfg.out_dir,
                   merge_terms(sess.cfg.slide_terms, sess.terms.fixed()),
                   fix_brief(sess.cfg.slide_brief, sess.cfg.slide_terms, sess.terms.fixed()))


def seg_html(seg, show_en: bool, latest: bool) -> str:
    if seg.translation is None:
        zh = '<span class="pending">翻譯中…</span>'
    elif seg.error:
        zh = f'<span class="err">{html.escape(seg.error)}</span>'
    else:
        zh = html.escape(seg.translation)
    en = f'<div class="en">{html.escape(seg.source)}</div>' if show_en else ""
    t = f'<div class="t">{fmt_time(seg.elapsed)}</div>'
    if latest:
        return f'<div class="latest">{t}<div class="zh">{zh}</div>{en}</div>'
    return f'<div class="seg">{t}<div class="zh">{zh}</div>{en}</div>'


@st.fragment(run_every=1.0)
def live_view():
    s: LiveSession | None = ss.session
    if s is None:
        st.html("""<div class="empty"><h3>準備上課</h3><ol>
            <li>左邊填<b>課程名稱</b>，上傳這堂課的<b>簡報</b>（建議）</li>
            <li>按上面的 <b>● 開始錄音</b>，中文字幕會出現在這裡</li>
            <li>下課按 <b>⏹ 停止</b>，再按 <b>📝 產生筆記</b></li></ol></div>""")
        return
    # 錄音結束（按停止或檔案播完）→ 重新整理整頁，讓按鈕狀態更新
    if st.session_state.get("was_running") and not s.running:
        st.session_state.was_running = False
        st.rerun()
    st.session_state.was_running = s.running
    segs, partial, status, errors, _ = s.snapshot()

    kind = "rec" if "錄音中" in status else ("wait" if s.running else "")
    label = status.replace("🔴 ", "")
    st.html(f'<div class="statusbar"><span class="pill {kind}"><span class="dot"></span>{html.escape(label)}</span>'
            f'<span><span class="k">錄音時間</span><span class="v">{fmt_time(s.elapsed())}</span></span>'
            f'<span><span class="k">辨識費用</span><span class="v">約 ${s.est_cost():.2f}</span></span></div>')
    if errors:
        with st.expander(f"⚠️ 訊息（{len(errors)}）"):
            st.text("\n".join(errors[-20:]))

    if partial and s.running:
        st.html(f'<div class="listening"><b>正在聽</b>{html.escape(partial)}</div>')
    rows = segs[-max_rows:]
    if newest_first:
        rows = rows[::-1]
    if rows:
        latest = rows[0] if newest_first else rows[-1]
        st.html("".join(seg_html(seg, show_en, seg is latest) for seg in rows))
    elif s.running:
        st.caption("等老師開口，第一句字幕幾秒後就會出現…")
    if not s.running and segs:
        st.caption(f"✓ 逐字稿已存檔：{s.transcript_path}")


live_view()

if ss.notes:
    st.divider()
    head_l, head_r = st.columns([3, 1], vertical_alignment="center")
    head_l.subheader("📝 筆記")
    head_r.download_button("下載 .md", ss.notes, file_name=Path(ss.notes_path).name, use_container_width=True)
    st.caption(f"已存檔：{ss.notes_path}")
    with st.container(border=True, key="notes"):
        # 粗體緊貼中文標點時 Markdown 不會解析（會看到 **），顯示時直接換成 HTML 粗體
        st.markdown(re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", ss.notes), unsafe_allow_html=True)

# 程式當掉、關掉後，也能用 records/ 裡存好的逐字稿產生筆記
saved = sorted((d for d in RECORDS_DIR.glob("*/") if d.is_dir()
                and (d / "逐字稿_原文.txt").exists() and (d / "逐字稿_原文.txt").stat().st_size > 0),
               reverse=True) if RECORDS_DIR.exists() else []
if saved:
    st.divider()
    with st.expander("📂 用之前的逐字稿產生筆記"):
        pick = st.selectbox("選擇課堂", saved,
                            format_func=lambda d: d.name + ("（已有筆記）" if (d / "筆記.md").exists() else ""))
        if st.button("產生筆記", disabled=running and sess.cfg.out_dir == pick):
            if not api_key:
                st.error("找不到 Mistral API Key：請照 README 建立 .env 檔，再重新啟動程式")
            else:
                # 資料夾名稱格式：日期_時間_課程名稱
                date_str, _, rest = pick.name.partition("_")
                course_name = rest.partition("_")[2]
                info = load_slides_info(pick)
                slide_terms, fixed = info.get("terms", {}), load_fixed_terms(course_name)
                make_notes((pick / "逐字稿_原文.txt").read_text(encoding="utf-8"), course_name, date_str, pick,
                           merge_terms(slide_terms, fixed), fix_brief(info.get("summary", ""), slide_terms, fixed))


# ---------------------------------------------------------------- 側邊欄：修正譯名、進階設定
@st.fragment
def terms_panel():
    s: LiveSession | None = ss.session
    # 有錄音（進行中或剛結束）就改那堂課；還沒開始就改這門課存起來的譯名
    book_course = s.cfg.course if s else course
    book = s.terms if s else TermBook(load_fixed_terms(course))

    st.markdown("#### 修正譯名")
    with st.form("fix_term", clear_on_submit=True, border=False):
        en = st.text_input("沒翻好的原文名詞", placeholder="例如：merit goods",
                           help="請填完整的名詞（例如 merit goods），不要只填 goods、board 這種常見單字，"
                                "否則一般用法也可能被套用")
        zh = st.text_input("正確的翻譯", placeholder="例如：有益財")
        if st.form_submit_button("套用", use_container_width=True):
            if not en.strip() or not zh.strip():
                st.warning("兩格都要填")
            else:
                book.fix(en, zh)
                save_fixed_terms(book_course, book.fixed())
                st.session_state.term_msg = f"已套用：{en.strip()} → {zh.strip()}"
                st.rerun(scope="fragment")
    if msg := st.session_state.pop("term_msg", None):
        st.success(msg)
    fixed = book.fixed()
    if fixed:
        st.caption("已修正，之後的翻譯和筆記會照用：\n" +
                   "\n".join(f"- {k} → {v}" for k, v in fixed.items()))


with terms_slot:
    terms_panel()
