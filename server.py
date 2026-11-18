# -*- coding: utf-8 -*-
"""DXF / DWG 互转工具 —— 本地可视化服务（仅监听 127.0.0.1）。

运行：python server.py   然后浏览器访问 http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
SETTINGS = os.path.join(ROOT, "settings.json")
UPLOADS = os.path.join(ROOT, "uploads")
DROP_OUTPUT = os.path.join(ROOT, "转换输出")
MAX_UPLOAD = 200 * 1024 * 1024
sys.path.insert(0, ROOT)

import converter  # noqa: E402
import dxf_svg  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PREVIEW_CACHE: dict[tuple, dict] = {}
MIME = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".png": "image/png",
}


def load_settings() -> dict:
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def list_roots() -> list[dict]:
    out = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            p = letter + ":\\"
            if os.path.isdir(p):
                out.append({"name": letter + ":", "path": p})
    else:
        out.append({"name": "/", "path": "/"})
    home = os.path.expanduser("~")
    if os.path.isdir(home):
        out.append({"name": "用户目录", "path": home})
        for label, folder in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents")):
            p = os.path.join(home, folder)
            if os.path.isdir(p):
                out.append({"name": label, "path": p})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "CADConverter/1.0"

    def log_message(self, fmt: str, *args) -> None:
        if "/api/" in (self.path or ""):
            sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 基础响应 ----------
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str) -> None:
        rel = "index.html" if rel in ("", "/") else rel.lstrip("/")
        path = os.path.normpath(os.path.join(WEB, rel))
        if not path.startswith(WEB) or not os.path.isfile(path):
            self._bytes(b"Not Found", "text/plain; charset=utf-8", 404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self._bytes(data, MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))

    def _query(self) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True).items()}

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        q = self._query()
        try:
            if path.startswith("/api/"):
                return self._api_get(path, q)
            return self._static(path)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def _api_get(self, path: str, q: dict) -> None:
        if path == "/api/roots":
            return self._json({"roots": list_roots()})
        if path == "/api/engines":
            s = load_settings()
            return self._json({"status": converter.engine_status(s.get("odaPath", "")),
                              "versions": {"dwg": converter.DWG_VERSIONS,
                                           "dxf": converter.DXF_VERSIONS,
                                           "oda": converter.ODA_VERSIONS},
                              "settings": s})
        if path == "/api/list":
            return self._json(self._list(q.get("path", ""), q.get("filter", "cad")))
        if path == "/api/info":
            return self._json(self._info(q.get("path", "")))
        if path == "/api/preview":
            return self._json(self._preview(q.get("path", "")))
        if path == "/api/download":
            return self._download(q.get("path", ""))
        self._json({"error": "未知接口"}, 404)

    def _list(self, raw: str, flt: str) -> dict:
        if not raw:
            return {"path": "", "parent": "", "dirs": [], "files": [], "roots": list_roots()}
        target = os.path.abspath(raw)
        if not os.path.isdir(target):
            return {"error": "目录不存在或不可访问：%s" % raw}
        dirs, files = [], []
        try:
            with os.scandir(target) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if e.name.startswith(".") or (os.name == "nt" and
                                    e.name.lower() in ("$recycle.bin", "system volume information")):
                                continue
                            dirs.append({"name": e.name, "path": e.path})
                        elif e.is_file():
                            if flt == "dirs":
                                continue
                            ext = os.path.splitext(e.name)[1].lower()
                            if flt == "cad" and ext not in (".dxf", ".dwg"):
                                continue
                            st = e.stat()
                            files.append({
                                "name": e.name, "path": e.path, "ext": ext,
                                "size": st.st_size,
                                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                            })
                    except (OSError, PermissionError):
                        continue
        except Exception as exc:
            return {"error": "无法读取目录：%s" % exc}
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())
        parent = os.path.dirname(target.rstrip(os.sep)) or target
        return {"path": target, "parent": parent, "dirs": dirs[:600],
                "files": files[:600], "total": len(files)}

    def _info(self, raw: str) -> dict:
        if not raw or not os.path.isfile(raw):
            return {"error": "文件不存在"}
        st = os.stat(raw)
        return {"path": os.path.abspath(raw), "name": os.path.basename(raw),
                "size": st.st_size, "ext": os.path.splitext(raw)[1].lower(),
                "version": converter.probe_version(raw),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}

    def _preview(self, raw: str) -> dict:
        if not raw or not os.path.isfile(raw):
            return {"error": "文件不存在"}
        ext = os.path.splitext(raw)[1].lower()
        if ext not in (".dxf", ".dwg"):
            return {"error": "不支持预览此文件类型"}
        st = os.stat(raw)
        key = (os.path.abspath(raw), st.st_mtime, st.st_size)
        if key in PREVIEW_CACHE:
            return PREVIEW_CACHE[key]
        src = raw
        tmpdir = None
        try:
            if ext == ".dwg":
                tmpdir = tempfile.mkdtemp(prefix="cadpv_", dir=converter._ascii_temp_parent())
                src = converter.preview_source_dxf(raw, tmpdir)
                if not src:
                    return {"error": "该 DWG 无法解析预览（可能是高版本或加密文件）"}
            result = dxf_svg.render(src)
            result["ok"] = True
        except Exception as exc:
            result = {"ok": False, "error": "预览失败：%s" % exc,
                      "svg": dxf_svg._empty_svg(1000, 620, "预览失败")}
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
        if len(PREVIEW_CACHE) > 40:
            PREVIEW_CACHE.clear()
        PREVIEW_CACHE[key] = result
        return result

    def _download(self, raw: str) -> None:
        if not raw or not os.path.isfile(raw) or os.path.splitext(raw)[1].lower() not in (".dxf", ".dwg"):
            return self._bytes(b"Forbidden", "text/plain; charset=utf-8", 403)
        with open(raw, "rb") as f:
            data = f.read()
        name = os.path.basename(raw)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" %
                         urllib.parse.quote(name))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- 拖拽上传 ----------
    def _upload(self) -> None:
        """接收浏览器拖入的文件（raw body + X-Cad-Name 头），存到本机 uploads/。

        浏览器出于安全限制拿不到拖拽文件的真实磁盘路径，只能上传文件本体；
        转换输出需由用户在界面上手动指定，兜底目录为 DROP_OUTPUT。
        """
        raw_name = urllib.parse.unquote(self.headers.get("X-Cad-Name", ""))
        name = os.path.basename(raw_name.replace("\\", "/"))
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".dxf", ".dwg"):
            return self._json({"error": "只支持 .dxf / .dwg 文件：%s" % name}, 400)
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_UPLOAD:
            return self._json({"error": "文件大小不合法或超过 200 MB"}, 400)
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.splitext(name)[0]) or "drawing"
        os.makedirs(UPLOADS, exist_ok=True)
        os.makedirs(DROP_OUTPUT, exist_ok=True)
        target = os.path.join(UPLOADS, stem + ext)
        seq = 1
        while os.path.exists(target):
            target = os.path.join(UPLOADS, "%s_%d%s" % (stem, seq, ext))
            seq += 1
        remaining = size
        with open(target, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            try:
                os.remove(target)
            except OSError:
                pass
            return self._json({"error": "文件接收不完整"}, 400)
        return self._json({"ok": True,
                           "path": os.path.abspath(target),
                           "name": os.path.basename(target),
                           "size": os.path.getsize(target),
                           "defaultOut": DROP_OUTPUT})

    # ---------- POST ----------
    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/upload":
            try:
                return self._upload()
            except Exception as exc:
                traceback.print_exc()
                return self._json({"error": str(exc)}, 500)
        body = self._body()
        try:
            if path == "/api/convert":
                src = str(body.get("input", ""))
                dst = str(body.get("output", ""))
                if not src or not dst:
                    return self._json({"error": "缺少输入或输出路径"})
                return self._json(converter.convert_one(src, dst, body))
            if path == "/api/check":
                items = body.get("items", [])
                out = []
                for it in items:
                    p = os.path.join(str(it.get("outputDir", "")), str(it.get("outputName", "")))
                    out.append({"input": it.get("input", ""), "output": p,
                                "exists": os.path.isfile(p)})
                return self._json({"items": out})
            if path == "/api/settings":
                old = load_settings()
                old.update({k: v for k, v in body.items()
                            if k in ("odaPath", "engine", "dwgVersion", "dxfVersion",
                                     "odaVersion", "onExists")})
                save_settings(old)
                return self._json({"ok": True, "settings": old})
            if path == "/api/reveal":
                p = str(body.get("path", ""))
                folder = p if os.path.isdir(p) else os.path.dirname(p)
                if folder and os.path.isdir(folder):
                    if os.name == "nt":
                        os.startfile(folder)  # noqa: S606
                    else:
                        subprocess.Popen(["xdg-open", folder])
                    return self._json({"ok": True})
                return self._json({"error": "目录不存在"})
            self._json({"error": "未知接口"}, 404)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)


def main() -> None:
    port = int(os.environ.get("CAD_PORT", "8765"))
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("端口全部被占用，无法启动服务")
        return
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("=" * 58)
    print("  DXF / DWG 互转工具已启动")
    print("  访问地址: " + url)
    print("  引擎目录: " + converter.LIBREDWG_DIR)
    print("  关闭此窗口即可停止服务")
    print("=" * 58)
    if os.environ.get("CAD_NO_BROWSER") != "1":
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
