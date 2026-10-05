"""
從筆記的專有名詞表做閃卡，匯出成 Anki、Quizlet 可以匯入的文字檔

正面：原文名詞；背面：中文譯名＋一句話解釋
"""
from __future__ import annotations

import re

Card = tuple[str, str, str]      # (原文, 中文, 解釋)


def _clean(cell: str) -> str:
    """去掉 Markdown 粗體、反引號，以及會弄壞匯入檔的 tab 和換行。"""
    cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell).replace("`", "")
    return re.sub(r"[\t\r\n]+", " ", cell).strip()


def extract_cards(notes_md: str) -> list[Card]:
    """
    找出筆記裡表頭第一欄是「原文」的表格（專有名詞表），每一列做成一張卡。
    同一個原文只留第一次出現的。
    """
    cards: list[Card] = []
    seen: set[str] = set()
    in_table = False
    for line in notes_md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            continue
        cells = [_clean(c) for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):     # |---|---| 分隔列
            continue
        if cells and cells[0] == "原文":                               # 專有名詞表的表頭
            in_table = True
            continue
        if in_table and len(cells) >= 2 and cells[0] and cells[0].lower() not in seen:
            seen.add(cells[0].lower())
            cards.append((cells[0], cells[1], cells[2] if len(cells) > 2 else ""))
    return cards


def to_anki(cards: list[Card], tag: str = "", deck: str = "") -> str:
    """
    Anki：檔案 → 匯入，直接選這個檔案。
    開頭的 # 設定會自動套用分隔符號、標籤，並放進指定的牌組（Anki 2.1.55 以後支援）。
    """
    header = ["#separator:tab", "#html:true"]
    if deck.strip():
        header.append(f"#deck:{_clean(deck)}")
    tag = re.sub(r"\s+", "_", tag.strip())
    if tag:
        header.append(f"#tags:{tag}")
    rows = [f"{en}\t{zh}<br><small>{expl}</small>" if expl else f"{en}\t{zh}" for en, zh, expl in cards]
    return "\n".join(header + rows) + "\n"


def to_quizlet(cards: list[Card]) -> str:
    """Quizlet：建立學習集 → 匯入，把內容整個貼上（詞語和定義之間是 tab，每張卡一行）。"""
    return "\n".join(f"{en}\t{zh} — {expl}" if expl else f"{en}\t{zh}" for en, zh, expl in cards) + "\n"
