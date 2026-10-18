@echo off
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist engine\realesrgan-ncnn-vulkan.exe .venv\Scripts\python setup_engine.py
start "" .venv\Scripts\pythonw app.py
