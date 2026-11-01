#!/bin/bash
# 双击即可打开 AI4S 人才地图（首次可能需要右键 → 打开）
cd "$(dirname "$0")"
python3 -m streamlit run app.py
