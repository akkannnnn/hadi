#!/bin/bash
cd "$(dirname "$0")"
pip3 install -r requirements.txt
python3 app.py &
sleep 2
open "http://127.0.0.1:8742/frontend/index.html"