from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_website_operator_reconciles(api_cluster):
    api_cluster.api.apply_file(ROOT / "examples" / "14-crd-operator.yaml")
    spec = importlib.util.spec_from_file_location("website_operator", ROOT / "examples" / "website_operator.py")
    operator = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(operator)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                operator.reconcile(api_cluster.api)
            except Exception:
                pass
            time.sleep(0.1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        api_cluster.wait_until(
            lambda: (api_cluster.get("deployments", "blog") or {}).get("status", {}).get("readyReplicas") == 2,
            timeout=15, description="website deployment")
        api_cluster.wait_until(
            lambda: (api_cluster.get("websites", "blog") or {}).get("status", {}).get("readyReplicas") == 2,
            timeout=10, description="website status")
        assert api_cluster.get("services", "blog") is not None
    finally:
        stop.set()
        thread.join(timeout=3)
