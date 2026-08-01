import json
import time
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "output" / "data"

UA = {"User-Agent": "Mozilla/5.0 (macro-dashboard; github.com/user01ju/macro_dashboard)"}


def http_get(url, timeout=30, retries=2, headers=None, **kw):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers={**UA, **(headers or {})}, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    raise last


def load_json(path, default):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
