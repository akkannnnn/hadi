"""
Enderun AI v2.0 — Uygulama Başlatıcı
PyWebView + uvicorn → native pencere, platform bağımsız.
"""
import sys
import os
import threading
import time
import urllib.request

# app.py'nin bulunduğu klasörü Python yoluna ekle
# Bu sayede 'backend' modülü her zaman bulunur
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import uvicorn
import webview

from backend.main import app as fastapi_app

HOST = "127.0.0.1"
PORT = 8742


def _fastapi_calistir():
    uvicorn.run(fastapi_app, host=HOST, port=PORT,
                log_level="warning", access_log=False)


def _sunucu_hazir(maks_deneme=150) -> bool:
    for _ in range(maks_deneme):
        try:
            urllib.request.urlopen(f"http://{HOST}:{PORT}/api/durum", timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


if __name__ == "__main__":
    t = threading.Thread(target=_fastapi_calistir, daemon=True)
    t.start()

    if not _sunucu_hazir():
        print("HATA: Sunucu 15 saniyede başlatılamadı.", file=sys.stderr)
        sys.exit(1)

    webview.create_window(
        title     = "Enderun AI — Ders Programı Hazırlama Motoru",
        url       = f"http://{HOST}:{PORT}/",
        width     = 1440,
        height    = 880,
        min_size  = (1024, 640),
        resizable = True,
    )
    webview.start(debug=False)
