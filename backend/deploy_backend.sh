#!/bin/sh
if [ ! -d myenv ]; then
    python3 -m venv myenv
fi
source myenv/bin/activate
pip install -r src/requirements.txt
pgrep -f '/opt/quant/backend/myenv/bin/python3' | xargs -r kill -9
sleep 1
ENV=prod uvicorn src.app.main:app --host 0.0.0.0 --port 8000 1>>/var/log/quant/backend.log 2>&1 &
