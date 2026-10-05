#!/bin/bash
# 在 Finder 雙擊這個檔案即可啟動
cd "$(dirname "$0")"
source .venv/bin/activate
streamlit run app.py
