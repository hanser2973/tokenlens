# -*- coding: utf-8 -*-
"""
TokenLens · AI / Agent Token 消耗统计看板
- 自动扫描本机 AI 工具日志采集真实 token 用量（见 collector.py）
- 也可手动录入数据
- 优先以原生窗口运行（pywebview），不可用时回退为浏览器打开
"""
import json
import os
import random
import socket
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import collector
from pricing import load_prices, calc_cost

if getattr(sys, "frozen", False):
    # 打包成 exe 后：静态资源在 exe 内（sys._MEIPASS），数据文件放在 exe 同目录
    BASE_DIR = os.path.dirname(sys.executable)
    STATIC_DIR = os.path.join(sys._MEIPASS, "static")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "records.json")
COLLECT_INTERVAL = 600  # 每 10 分钟自动扫描一次本地日志

_lock = threading.Lock()

# 供下拉列表使用的常见 Agent 名（实际数据以扫描结果为准）
KNOWN_AGENTS = ["Kimi Code", "Claude Code", "Codex CLI", "Cursor", "Cline", "Trae", "Gemini CLI", "Aider"]


def load_records():
    with _lock:
        if not os.path.exists(DATA_FILE):
            save_records([])
            return []
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            return []


def save_records(records):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)


def seed_demo_data():
    """仅在使用 --demo 启动时生成演示数据。"""
    rng = random.Random(42)
    prices = load_prices(DATA_DIR)
    agents_models = {
        "Kimi Code": ["Kimi K2"], "Claude Code": ["Claude Sonnet 4.5"],
        "Cursor": ["GPT-5.2", "Gemini 3 Pro"], "Codex CLI": ["GPT-5.2"],
        "Cline": ["DeepSeek V3.2", "Qwen3 Coder"], "Trae": ["Doubao Seed 1.6"],
    }
    records, rid, today = [], 1, date.today()
    for d in range(45, 0, -1):
        day = today - timedelta(days=d)
        factor = 0.45 if day.weekday() >= 5 else 1.0
        for agent, models in agents_models.items():
            if rng.random() > 0.75 * factor:
                continue
            for model in models:
                if rng.random() > 0.55:
                    continue
                req = rng.randint(3, 40)
                inp = int(req * rng.uniform(1800, 9000))
                out = int(inp * rng.uniform(0.15, 0.45))
                cached = int(inp * rng.uniform(0.0, 0.35))
                records.append({
                    "id": rid, "date": day.isoformat(), "agent": agent, "model": model,
                    "requests": req, "input": inp, "output": out, "cached": cached,
                    "cost": calc_cost(prices, model, inp, out, cached), "source": "demo",
                })
                rid += 1
    save_records(records)


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def merged_records():
    prices = load_prices(DATA_DIR)
    manual = load_records()
    for r in manual:
        r.setdefault("source", "manual")
    auto = collector.load_auto_records(DATA_DIR, prices, calc_cost)
    return manual + auto, prices


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            self._send_file(os.path.join(STATIC_DIR, "index.html"))
        elif path == "/api/records":
            records, prices = merged_records()
            self._send_json({"records": records, "prices": prices, "agents": KNOWN_AGENTS})
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR):
                self._send_json({"error": "forbidden"}, 403)
                return
            self._send_file(full)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/collect":
            try:
                summary = collector.collect_once(DATA_DIR)
            except Exception as e:  # 采集失败不影响主程序
                self._send_json({"error": str(e)}, 500)
                return
            self._send_json({"ok": True, "summary": summary})
            return
        if url.path != "/api/records":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json({"error": "invalid json"}, 400)
            return

        required = ["date", "agent", "model"]
        if any(not str(payload.get(k, "")).strip() for k in required):
            self._send_json({"error": "date/agent/model 不能为空"}, 400)
            return

        def to_int(v):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return 0

        records = load_records()
        new_id = max((r["id"] for r in records if isinstance(r.get("id"), int)), default=0) + 1
        input_t = to_int(payload.get("input"))
        output_t = to_int(payload.get("output"))
        cached_t = to_int(payload.get("cached"))
        model = str(payload["model"]).strip()
        cost = payload.get("cost")
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = calc_cost(load_prices(DATA_DIR), model, input_t, output_t, cached_t)

        rec = {
            "id": new_id,
            "date": str(payload["date"]).strip(),
            "agent": str(payload["agent"]).strip(),
            "model": model,
            "requests": to_int(payload.get("requests")) or 1,
            "input": input_t,
            "output": output_t,
            "cached": cached_t,
            "cost": round(cost, 4),
            "source": "manual",
        }
        with _lock:
            records.append(rec)
            save_records(records)
        self._send_json({"ok": True, "record": rec})

    def do_DELETE(self):
        url = urlparse(self.path)
        if url.path != "/api/records":
            self._send_json({"error": "not found"}, 404)
            return
        qs = parse_qs(url.query)
        try:
            rid = int(qs.get("id", [""])[0])
        except ValueError:
            self._send_json({"error": "自动采集的记录不能删除"}, 400)
            return
        records = load_records()
        before = len(records)
        with _lock:
            records = [r for r in records if r.get("id") != rid]
            if len(records) == before:
                self._send_json({"error": "记录不存在"}, 404)
                return
            save_records(records)
        self._send_json({"ok": True})


def collect_loop():
    """启动后立即采集一次，之后定时增量扫描。"""
    while True:
        try:
            collector.collect_once(DATA_DIR)
        except Exception:
            pass
        time.sleep(COLLECT_INTERVAL)


def find_free_port(preferred):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", preferred))
        return preferred
    except OSError:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def main():
    if "--demo" in sys.argv:
        seed_demo_data()
        print("已生成演示数据")

    load_records()  # 确保数据文件存在
    threading.Thread(target=collect_loop, daemon=True).start()

    port = find_free_port(8399)
    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
        webview.create_window("TokenLens", url, width=1440, height=900, min_size=(1100, 700))
        webview.start()  # 窗口关闭后返回
        server.shutdown()
        return
    except ImportError:
        pass

    # 无 pywebview 时回退为浏览器打开
    import webbrowser
    print("=" * 52)
    print("  TokenLens · AI / Agent Token 消耗看板")
    print(f"  已在浏览器中打开: {url}")
    print("  关闭本窗口即可停止服务")
    print("=" * 52)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
