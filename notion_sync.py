"""
把筆記傳到 Notion（用 Notion 官方 API，不需要 MCP）

設定方式見 README：建立 integration、把金鑰寫進 .env 的 NOTION_TOKEN、
把「課堂筆記」資料庫分享給這個 integration。
"""
from __future__ import annotations

import re
COURSE_PROP = "課程"
DATE_PROP = "日期"
TEXT_LIMIT = 2000        # Notion 每段文字最多 2000 字
BATCH = 100              # 每次最多送 100 個區塊


def _client(token: str):
    from notion_client import Client
    return Client(auth=token)


def find_databases(token: str) -> list[tuple[str, str]]:
    """列出分享給這個 integration 的資料庫，回傳 [(data_source_id, 名稱)]。"""
    res = _client(token).search(filter={"property": "object", "value": "data_source"}, page_size=50)
    out = []
    for ds in res.get("results", []):
        title = "".join(t.get("plain_text", "") for t in ds.get("title", [])) or "（未命名資料庫）"
        out.append((ds["id"], title))
    return out


def _ensure_properties(client, ds_id: str) -> str:
    """確認資料庫有「課程」「日期」欄位（沒有就自動新增），回傳標題欄位的名稱。"""
    props = client.data_sources.retrieve(data_source_id=ds_id)["properties"]
    title_prop = next(name for name, p in props.items() if p["type"] == "title")
    missing = {}
    if COURSE_PROP not in props:
        missing[COURSE_PROP] = {"select": {}}
    if DATE_PROP not in props:
        missing[DATE_PROP] = {"date": {}}
    if missing:
        client.data_sources.update(data_source_id=ds_id, properties=missing)
    return title_prop


# ---------------------------------------------------------------------------
# Markdown → Notion 區塊（只處理筆記會用到的：標題、清單、表格、段落、粗體）
# ---------------------------------------------------------------------------
def _rich(text: str) -> list[dict]:
    parts = []
    for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not chunk:
            continue
        for start in range(0, len(chunk), TEXT_LIMIT):
            parts.append({"type": "text", "text": {"content": chunk[start:start + TEXT_LIMIT]},
                          "annotations": {"bold": i % 2 == 1}})
    return parts


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich(text)}}


def _table(rows: list[list[str]]) -> dict:
    width = max(len(r) for r in rows)
    return {"object": "block", "type": "table",
            "table": {"table_width": width, "has_column_header": True, "has_row_header": False,
                      "children": [{"object": "block", "type": "table_row",
                                    "table_row": {"cells": [_rich(c) for c in r + [""] * (width - len(r))]}}
                                   for r in rows]}}


def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    table: list[list[str]] = []
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):   # 跳過 |---|---| 分隔列
                table.append(cells)
            continue
        if table:
            blocks.append(_table(table))
            table = []
        if not s or re.fullmatch(r"-{3,}|\*{3,}", s):
            continue
        if m := re.match(r"(#{1,6})\s+(.*)", s):
            blocks.append(_block(f"heading_{min(len(m.group(1)), 3)}", m.group(2)))
        elif m := re.match(r"[-*•]\s+(.*)", s):
            blocks.append(_block("bulleted_list_item", m.group(1)))
        elif m := re.match(r"\d+[.)]\s+(.*)", s):
            blocks.append(_block("numbered_list_item", m.group(1)))
        else:
            blocks.append(_block("paragraph", s))
    if table:
        blocks.append(_table(table))
    return blocks


def push_notes(token: str, ds_id: str, course: str, date_str: str, notes_md: str) -> str:
    """在資料庫建立一頁筆記，回傳頁面網址。"""
    client = _client(token)
    title_prop = _ensure_properties(client, ds_id)
    blocks = markdown_to_blocks(notes_md)
    props = {title_prop: {"title": _rich(f"{course or '未命名課程'}｜{date_str}")}}
    if course:
        props[COURSE_PROP] = {"select": {"name": course[:100].replace(",", "，")}}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        props[DATE_PROP] = {"date": {"start": date_str}}
    page = client.pages.create(parent={"type": "data_source_id", "data_source_id": ds_id},
                               properties=props, children=blocks[:BATCH])
    for i in range(BATCH, len(blocks), BATCH):
        client.blocks.children.append(block_id=page["id"], children=blocks[i:i + BATCH])
    return page.get("url", "")

