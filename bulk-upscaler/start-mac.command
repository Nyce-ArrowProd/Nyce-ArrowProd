#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
[ -f engine/realesrgan-ncnn-vulkan ] || .venv/bin/python setup_engine.py
.venv/bin/python app.py
