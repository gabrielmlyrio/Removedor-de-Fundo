@echo off
cd /d "%~dp0onemus-server"
python -m pip install -r requirements.txt -q
set HOST=127.0.0.1
set PORT=8765
echo Abrindo One For All online em http://127.0.0.1:8765/
start "" "http://127.0.0.1:8765/"
python server.py
