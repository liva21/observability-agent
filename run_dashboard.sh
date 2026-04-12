#!/bin/bash
# Dashboard'u başlatan yardımcı script
# Mac'te uvicorn PATH sorununun önüne geçer

export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:$PATH"
export PYTHONPATH="$(pwd)"

echo "Dashboard başlatılıyor → http://localhost:8080"
echo "Swagger UI          → http://localhost:8080/docs"
echo "React Dashboard     → http://localhost:8080/static/index.html"
echo ""

python3 -m uvicorn src.dashboard.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --reload
