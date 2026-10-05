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

import notion_sync
from core import (DEFAULT_NOTES_MODEL, DEFAULT_STT_MODEL, DEFAULT_TRANSLATE_MODEL, SLIDES_MAX_PARTS,
                  LiveSession, SessionConfig, TermBook, analyze_slides, extract_slide_text, fix_brief,
                  fmt_time, generate_notes, list_input_devices, merge_terms, split_slide_text)

APP_DIR = Path(__file__).parent
RECORDS_DIR = APP_DIR / "records"
TERMS_DIR = APP_DIR / "專有名詞"


def load_env_key(name: str = "MISTRAL_API_KEY") -> str:
    """優先讀環境變數，其次讀同資料夾的 .env 檔。"""
    if os.getenv(name):
        return os.environ[name]
    env = APP_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(name):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "Untitled course"


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


st.set_page_config(page_title="Live Lecture Translator", page_icon="🎧", layout="wide")

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
    notes_meta: tuple[str, str] = ("", "")    # 筆記的（課程名稱, 日期），傳到 Notion 用
    notion_url: str | None = None


@st.cache_resource
def get_shared_state() -> SharedState:
    return SharedState()


ss = get_shared_state()

# ---------------------------------------------------------------- 側邊欄：上課前的設定
with st.sidebar:
    # 金鑰只從 .env（或環境變數）讀取，留在本機程式裡，不送到瀏覽器
    api_key = load_env_key()
    if not api_key:
        st.error("Mistral API key not found: create a .env file as described in the README, then restart the app")

    st.markdown("#### This class")
    course = st.text_input("Course name", placeholder="e.g. Strategies in the arts")

    # 上課前讀簡報：整理課程背景和專有名詞，翻譯和筆記都會用到。同一個檔案只分析一次
    slides_file = st.file_uploader("Lecture slides (recommended)", type=["pdf", "pptx"],
                                   help="Upload the PDF or PPTX before class. Translations will use the field's terminology and the standard translations of terms in the slides")
    slides = None
    if slides_file is not None:
        data = slides_file.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        cached = st.session_state.get("slides")
        if cached and cached["digest"] == digest:
            slides = cached
        elif api_key:
            try:
                slide_text = extract_slide_text(slides_file.name, data)
                n_parts = len(split_slide_text(slide_text))
                label = ("Reading slides… (about 10 seconds)" if n_parts == 1 else
                         f"Reading long slides in {min(n_parts, SLIDES_MAX_PARTS)} parts… (about 10–20 seconds)")
                with st.spinner(label):
                    brief, terms, warning = analyze_slides(api_key, slide_text)
                    slides = {"digest": digest, "name": slides_file.name, "summary": brief, "terms": terms,
                              "warning": warning, "pages": len(re.findall(r"\[第 \d+ 頁\]", slide_text)),
                              "parts": n_parts}
                    st.session_state.slides = slides
            except Exception as e:
                st.warning(f"Couldn't read these slides: {e}")
        if slides:
            with st.container(border=True):
                if slides.get("warning"):
                    st.warning(slides["warning"], icon="⚠️")
                elif slides.get("parts", 1) > 1:
                    st.caption(f"Read all {slides['pages']} slides in {slides['parts']} parts")
                st.caption(f"📖 {slides['summary']}")
                with st.popover(f"{len(slides['terms'])} terms from the slides", use_container_width=True):
                    st.markdown("\n".join(f"- {en} → **{zh}**" for en, zh in slides["terms"].items()) or "(none)")

    st.markdown("#### Audio")
    source = st.segmented_control("Audio source", ["Microphone", "Audio file"], default="Microphone",
                                  label_visibility="collapsed") or "Microphone"
    device, wav_path, wav_speed = None, None, 1.0
    if source == "Microphone":
        try:
            devices = list_input_devices()
            names = ["System default microphone"] + [f"{i}: {n}" for i, n in devices]
            pick = st.selectbox("Microphone", names, label_visibility="collapsed")
            if pick != names[0]:
                device = int(pick.split(":")[0])
        except Exception as e:
            st.warning(f"Couldn't list microphones: {e}")
    else:
        up = st.file_uploader("WAV file (16-bit), to simulate a class", type=["wav"])
        wav_speed = st.slider("Playback speed", 1.0, 4.0, 1.0, 0.5,
                              help="1.0 = real-time. Faster is handy for testing, but cost is still based on the audio length")
        if up is not None:
            tmp = APP_DIR / "records" / "_upload.wav"
            # 只有換了新檔案才寫入，避免每次操作介面都重寫一次
            if st.session_state.get("uploaded_id") != up.file_id or not tmp.exists():
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(up.getvalue())
                st.session_state.uploaded_id = up.file_id
            wav_path = str(tmp)
    save_audio = st.toggle("Also save the recording (FLAC)", value=True,
                           help="Saved in this class's folder so you can listen again later. About 60–90 MB per hour; doesn't affect recognition or cost")

    terms_slot = st.container()   # 修正譯名（函式在檔案最後定義）

    with st.expander("Advanced settings"):
        stt_model = st.text_input("Speech recognition model", DEFAULT_STT_MODEL)
        trans_model = st.text_input("Translation model", DEFAULT_TRANSLATE_MODEL)
        notes_model = st.text_input("Notes model", DEFAULT_NOTES_MODEL)
        delay_opt = st.selectbox("Recognition delay (ms)", ["Default", "240", "480", "960", "1600", "2400"],
                                 help="Shorter delay shows subtitles sooner; longer delay is usually more accurate")
        min_chars = st.slider("Minimum characters per translated segment", 30, 200, 80, 10,
                              help="Smaller numbers show subtitles sooner but send more translation requests")
        newest_first = st.checkbox("Show newest first", value=True)
        max_rows = st.slider("Max segments on screen", 10, 200, 40, 10)

# ---------------------------------------------------------------- 主畫面
sess: LiveSession | None = ss.session
running = bool(sess and sess.running)

shown_course = (sess.cfg.course if sess else course) or "Untitled course"
st.html(f'<div class="app-head"><div class="app-title">🎧 Live Lecture Translator</div>'
        f'<div class="app-sub">{html.escape(shown_course)}</div></div>')
b1, b2, b3 = st.columns([1, 1, 1.2], vertical_alignment="center")
start_clicked = stop_clicked = False
if running:
    stop_clicked = b1.button("⏹ Stop", use_container_width=True)
else:
    start_clicked = b1.button("● Start recording", type="primary", use_container_width=True)
can_note = bool(sess and not sess.running and sess.segments)
note_clicked = b2.button("📝 Generate notes", disabled=not can_note, use_container_width=True)
show_en = b3.toggle("Show original text", value=True)

if start_clicked:
    if not api_key:
        st.error("Mistral API key not found: create a .env file as described in the README, then restart the app")
    elif source != "Microphone" and not wav_path:
        st.error("Upload a WAV file in the sidebar first")
    else:
        out_dir = RECORDS_DIR / f"{datetime.now():%Y-%m-%d_%H%M}_{safe_name(course)}"
        cfg = SessionConfig(api_key=api_key, out_dir=out_dir, course=course,
                            stt_model=stt_model, translate_model=trans_model,
                            target_delay_ms=None if delay_opt == "Default" else int(delay_opt),
                            input_device=device, wav_path=wav_path, wav_speed=wav_speed,
                            min_chars=min_chars, fixed_terms=load_fixed_terms(course),
                            slide_brief=slides["summary"] if slides else "",
                            slide_terms=slides["terms"] if slides else {}, save_audio=save_audio)
        ss.session = LiveSession(cfg)
        if slides:   # 存一份，之後從舊逐字稿產生筆記時也能用
            (out_dir / SLIDES_FILE).write_text(json.dumps(
                {"name": slides["name"], "summary": slides["summary"], "terms": slides["terms"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
        ss.notes = ss.notes_path = ss.notion_url = None
        ss.session.start()
        st.rerun()

if stop_clicked:
    sess.stop()
    st.rerun()


def make_notes(transcript: str, course: str, date_str: str, out_dir: Path,
               glossary: dict[str, str], background: str):
    """產生筆記並存到 out_dir/筆記.md。分段結果先暫存，失敗後再按一次會接續。"""
    cache_dir = out_dir / ".筆記暫存"
    with st.status("Generating notes…", expanded=True) as box:
        try:
            notes = generate_notes(api_key, transcript, course, date_str, model=notes_model,
                                   progress=box.write, cache_dir=cache_dir, glossary=glossary, background=background)
            path = out_dir / "筆記.md"
            path.write_text(notes, encoding="utf-8")
            shutil.rmtree(cache_dir, ignore_errors=True)
            ss.notes, ss.notes_path = notes, path
            ss.notes_meta, ss.notion_url = (course, date_str), None
            box.update(label="Notes ready — see below the subtitles", state="complete", expanded=False)
        except Exception as e:
            box.update(label=f"Generating notes failed: {e} (finished parts are saved; click again to resume)", state="error")


if note_clicked:
    if not api_key:
        st.error("Mistral API key not found: create a .env file as described in the README, then restart the app")
    else:
        make_notes(sess.full_source_text(), sess.cfg.course,
                   f"{datetime.fromtimestamp(sess.started_at):%Y-%m-%d}", sess.cfg.out_dir,
                   merge_terms(sess.cfg.slide_terms, sess.terms.fixed()),
                   fix_brief(sess.cfg.slide_brief, sess.cfg.slide_terms, sess.terms.fixed()))


def seg_html(seg, show_en: bool, latest: bool) -> str:
    if seg.translation is None:
        zh = '<span class="pending">Translating…</span>'
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
        st.html("""<div class="empty"><h3>Ready for class</h3><ol>
            <li>Enter the <b>course name</b> in the sidebar and upload the <b>slides</b> (recommended)</li>
            <li>Click <b>● Start recording</b> above — Chinese subtitles will appear here</li>
            <li>After class, click <b>⏹ Stop</b>, then <b>📝 Generate notes</b></li></ol></div>""")
        return
    # 錄音結束（按停止或檔案播完）→ 重新整理整頁，讓按鈕狀態更新
    if st.session_state.get("was_running") and not s.running:
        st.session_state.was_running = False
        st.rerun()
    st.session_state.was_running = s.running
    segs, partial, status, errors, _ = s.snapshot()

    kind = "rec" if status == "Recording" else ("wait" if s.running else "")
    label = status.replace("🔴 ", "")
    st.html(f'<div class="statusbar"><span class="pill {kind}"><span class="dot"></span>{html.escape(label)}</span>'
            f'<span><span class="k">Time</span><span class="v">{fmt_time(s.elapsed())}</span></span>'
            f'<span><span class="k">Recognition cost</span><span class="v">≈ ${s.est_cost():.2f}</span></span></div>')
    if errors:
        with st.expander(f"⚠️ Messages ({len(errors)})"):
            st.text("\n".join(errors[-20:]))

    if partial and s.running:
        st.html(f'<div class="listening"><b>Listening</b>{html.escape(partial)}</div>')
    rows = segs[-max_rows:]
    if newest_first:
        rows = rows[::-1]
    if rows:
        latest = rows[0] if newest_first else rows[-1]
        st.html("".join(seg_html(seg, show_en, seg is latest) for seg in rows))
    elif s.running:
        st.caption("Waiting for the lecturer — the first subtitle appears a few seconds after they start…")
    if not s.running and segs:
        st.caption(f"✓ Transcript saved: {s.transcript_path}")


live_view()

if ss.notes:
    st.divider()
    head_l, head_m, head_r = st.columns([2.4, 1, 1.2], vertical_alignment="center")
    head_l.subheader("📝 Notes")
    head_m.download_button("Download .md", ss.notes, file_name=Path(ss.notes_path).name, use_container_width=True)
    notion_token = load_env_key("NOTION_TOKEN")
    if notion_token:
        to_notion = head_r.button("Send to Notion", type="primary", use_container_width=True)
    else:
        to_notion = False
        head_r.caption("Set up Notion to send notes there (see README)")
    st.caption(f"Saved: {ss.notes_path}")
    if to_notion:
        with st.spinner("Sending to Notion…"):
            try:
                # 先找名稱有「筆記」的資料庫；沒有的話，在分享給 integration 的頁面（例如 NEOMA）裡自動建立一個
                ds_id = notion_sync.find_notes_database(notion_token)
                pages = [] if ds_id else notion_sync.find_shared_pages(notion_token)
                if not ds_id and len(pages) == 1:
                    ds_id = notion_sync.create_notes_database(notion_token, pages[0][0])
                if ds_id:
                    ss.notion_url = notion_sync.push_notes(notion_token, ds_id, *ss.notes_meta, ss.notes)
                elif not pages:
                    st.error("No Notion page is shared with this integration: open the page where notes should go → \"⋯\" (top right) → "
                             "\"Connections\" → add your integration")
                else:
                    st.error("Several pages are shared with the integration (" + ", ".join(t for _, t in pages) +
                             "), so it's unclear where to put notes. Create a database with \"筆記\" in its name on the page you want")
            except Exception as e:
                st.error(f"Sending to Notion failed: {e}")
    if ss.notion_url:
        st.success(f"Sent to Notion: [open page]({ss.notion_url})")
    with st.container(border=True, key="notes"):
        # 粗體緊貼中文標點時 Markdown 不會解析（會看到 **），顯示時直接換成 HTML 粗體
        st.markdown(re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", ss.notes), unsafe_allow_html=True)

# 程式當掉、關掉後，也能用 records/ 裡存好的逐字稿產生筆記
saved = sorted((d for d in RECORDS_DIR.glob("*/") if d.is_dir()
                and (d / "逐字稿_原文.txt").exists() and (d / "逐字稿_原文.txt").stat().st_size > 0),
               reverse=True) if RECORDS_DIR.exists() else []
if saved:
    st.divider()
    with st.expander("📂 Generate notes from an earlier transcript"):
        pick = st.selectbox("Class", saved,
                            format_func=lambda d: d.name + (" (has notes)" if (d / "筆記.md").exists() else ""))
        if st.button("Generate notes", disabled=running and sess.cfg.out_dir == pick):
            if not api_key:
                st.error("Mistral API key not found: create a .env file as described in the README, then restart the app")
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

    st.markdown("#### Fix a translation")
    with st.form("fix_term", clear_on_submit=True, border=False):
        en = st.text_input("Term (original)", placeholder="e.g. merit goods",
                           help="Enter the full term (e.g. merit goods), not a common word like goods or board — "
                                "otherwise everyday uses of the word may be changed too")
        zh = st.text_input("Correct translation", placeholder="e.g. 有益財")
        if st.form_submit_button("Apply", use_container_width=True):
            if not en.strip() or not zh.strip():
                st.warning("Fill in both fields")
            else:
                book.fix(en, zh)
                save_fixed_terms(book_course, book.fixed())
                st.session_state.term_msg = f"Applied: {en.strip()} → {zh.strip()}"
                st.rerun(scope="fragment")
    if msg := st.session_state.pop("term_msg", None):
        st.success(msg)
    fixed = book.fixed()
    if fixed:
        st.caption("Fixed (used in later translations and notes):\n" +
                   "\n".join(f"- {k} → {v}" for k, v in fixed.items()))


with terms_slot:
    terms_panel()
