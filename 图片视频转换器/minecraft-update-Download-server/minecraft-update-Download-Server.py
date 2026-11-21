# -*- coding: utf-8 -*-
"""
桌面版局域网文件共享（Tkinter GUI + ThreadingHTTPServer）
- 优先绑定 Radmin VPN 虚拟网卡地址，也支持 0.0.0.0 通配绑定
- IP 不可用时自动降级：绑 0.0.0.0 / 手动指定本机地址 / 取消
- 纯桌面窗口，无网页后台
- 可视化实时日志栏
- 多线程高并发，多人同时下载互不影响
- 支持断点续传（Range）
- 支持共享整个文件夹（递归子目录，可逐层浏览/下载）
- 提供 /api/list JSON 接口，供桌面客户端对接
- 已适配 PyInstaller 打包（onefile / onedir 均可）
仅用 Python 标准库，Python 3.10+ 可直接运行
"""
import os
import sys
import json
import time
import queue
import socket
import shutil
import traceback
import threading
import mimetypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from datetime import datetime
from urllib.parse import unquote, quote

APP_TITLE = "局域网文件共享 · 服务端"
APP_VERSION = "1.3.1"
CHUNK_SIZE = 1024 * 256          # 流式分块 256KB

# ================== 网络配置 ==================
# 绑定地址：
#   "0.0.0.0"        → 监听所有网卡（推荐，无需确认本机是否具备某 IP）
#   "26.5.184.68"    → 只监听指定网卡（该 IP 必须是本机已有地址）
FIXED_IP = "0.0.0.0"
FIXED_PORT = 1024


# ================== 路径处理（PyInstaller 兼容） ==================
def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()


# ================== 日志总线 ==================
class LogBus:
    def __init__(self, maxlen=5000):
        self.q = queue.Queue()
        self.all_logs = []
        self.maxlen = maxlen

    def put(self, level, msg):
        rec = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        }
        self.all_logs.append(rec)
        if len(self.all_logs) > self.maxlen:
            self.all_logs = self.all_logs[-self.maxlen:]
        self.q.put(rec)


LOG = LogBus()


def log(level, msg):
    LOG.put(level, msg)


def human_size(n):
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or u == "TB":
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024


def is_wildcard_ip(ip):
    """
    判断是否为通配/特殊绑定地址。
    这类地址不需要出现在本机网卡列表中，可直接用于 bind()。
    """
    return ip in ("0.0.0.0", "", "::", "127.0.0.1", "localhost")


def list_local_ips():
    """枚举本机所有 IPv4 地址，用于校验固定 IP 是否存在"""
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.add("127.0.0.1")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return ips


def check_local_ip(target_ip):
    """
    检查指定 IP 是否可用于绑定。
    - 通配地址（0.0.0.0 等）直接视为可用
    - 其他地址需存在于本机网卡
    返回 (ok, 本机IP列表)
    """
    ips = list_local_ips()
    if is_wildcard_ip(target_ip):
        return True, sorted(ips)
    return (target_ip in ips), sorted(ips)


def probe_port(bind_ip, port):
    """
    预检端口是否可绑定，不真正启动服务。
    返回 (ok, 错误信息)
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((bind_ip, port))
        return True, ""
    except PermissionError:
        return False, f"端口 {port} 权限不足（需管理员权限或落在系统保留段）"
    except OSError as e:
        return False, f"端口 {port} 无法绑定：{e}"
    finally:
        s.close()


def safe_relpath(path):
    """
    把 URL 中的相对路径规范化，阻止目录穿越。
    返回归一化后的相对路径列表，非法则返回 None。
    """
    path = path.replace("\\", "/").strip("/")
    if not path:
        return []
    parts = []
    for seg in path.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        if any(c in seg for c in '<>:"|?*') or "\0" in seg:
            return None
        parts.append(seg)
    return parts


# ================== HTTP 处理 ==================
class ShareHandler(BaseHTTPRequestHandler):
    share_dir = ""
    server_version = "FileShare/2.3"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _client(self):
        return f"{self.client_address[0]}:{self.client_address[1]}"

    # ---------- 路由 ----------
    def do_GET(self):
        raw = unquote(self.path.split("?", 1)[0])

        # ---- JSON API：桌面客户端专用 ----
        if raw.startswith("/api/list"):
            rel = raw[len("/api/list"):]
            parts = safe_relpath(rel)
            if parts is None:
                self._send_json({"error": "非法路径"}, 400)
                return
            self._send_api_list(parts)
            return

        if raw == "/api/ping":
            self._send_json({"ok": True, "version": self.server_version})
            return

        # ---- 网页浏览 ----
        if raw in ("/", "/index.html", "/list"):
            self._send_browse_page([])
            return

        if raw.startswith("/browse/"):
            parts = safe_relpath(raw[len("/browse/"):])
            if parts is None:
                self._send_error_page(400, "非法路径")
                return
            self._send_browse_page(parts)
            return

        if raw.startswith("/download/"):
            parts = safe_relpath(raw[len("/download/"):])
            if parts is None:
                self._send_error_page(400, "非法路径")
                return
            self._send_file(parts)
            return

        self._send_error_page(404, "未找到该资源")

    # ---------- 路径解析 ----------
    def _resolve(self, parts):
        target = os.path.abspath(os.path.join(self.share_dir, *parts))
        root = os.path.abspath(self.share_dir)
        try:
            if os.path.commonpath([target, root]) != root:
                return None
        except ValueError:
            return None
        return target

    # ---------- JSON 工具 ----------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _send_api_list(self, parts):
        """返回目录内容的 JSON：{path, dirs:[], files:[{name,size,mtime}]}"""
        cur = self._resolve(parts)
        if cur is None or not os.path.isdir(cur):
            self._send_json({"error": "目录不存在"}, 404)
            return

        dirs, files = [], []
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.name.startswith(".") or entry.name.startswith("$"):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    if entry.is_dir():
                        dirs.append(entry.name)
                    else:
                        files.append({
                            "name": entry.name,
                            "size": st.st_size,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        })
        except Exception as e:
            self._send_json({"error": f"读取失败: {e}"}, 500)
            return

        dirs.sort(key=str.lower)
        files.sort(key=lambda x: x["name"].lower())
        self._send_json({
            "path": "/" + "/".join(parts),
            "dirs": dirs,
            "files": files,
        })

    # ---------- 目录浏览页 ----------
    def _send_browse_page(self, parts):
        cur_dir = self._resolve(parts)
        if cur_dir is None or not os.path.isdir(cur_dir):
            self._send_error_page(404, "目录不存在")
            return

        dirs = []
        files = []
        try:
            with os.scandir(cur_dir) as it:
                for entry in it:
                    if entry.name.startswith("$"):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                    if entry.is_dir():
                        dirs.append((entry.name, entry.name, mtime))
                    else:
                        files.append((entry.name, st.st_size, mtime))
        except Exception as e:
            self._send_error_page(500, f"读取目录失败: {e}")
            return

        dirs.sort(key=lambda x: x[0].lower())
        files.sort(key=lambda x: x[0].lower())

        crumbs = ['<a href="/">根目录</a>']
        acc = []
        for p in parts:
            acc.append(p)
            href = "/browse/" + quote("/".join(acc))
            crumbs.append(f'<a href="{href}">{p}</a>')
        crumb_html = " / ".join(crumbs)

        parent_row = ""
        if parts:
            parent_href = "/" if len(parts) == 1 else "/browse/" + quote("/".join(parts[:-1]))
            parent_row = (f'<tr><td><a href="{parent_href}">⬆ 返回上一级</a></td>'
                          f'<td>—</td><td>—</td></tr>')

        rows = [parent_row]
        for name, _, mtime in dirs:
            sub = "/".join(parts + [name])
            href = "/browse/" + quote(sub)
            rows.append(
                f'<tr class="dir"><td><a href="{href}">📁 {name}/</a></td>'
                f'<td>—</td><td>{mtime}</td></tr>'
            )
        for name, size, mtime in files:
            sub = "/".join(parts + [name])
            href = "/download/" + quote(sub)
            rows.append(
                f'<tr><td><a href="{href}">📄 {name}</a></td>'
                f'<td>{human_size(size)}</td><td>{mtime}</td></tr>'
            )

        total_items = len(dirs) + len(files)
        tip = "" if total_items else (
            '<p style="color:#9ca3af;text-align:center;padding:30px">此目录为空</p>'
        )

        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>文件共享</title>
<style>
body{{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:960px;margin:32px auto;padding:0 16px;color:#1f2937}}
h2{{font-size:18px;margin:0 0 8px}}
.crumb{{font-size:13px;color:#6b7280;margin-bottom:14px;word-break:break-all}}
.crumb a{{color:#1f6feb;text-decoration:none}}
.crumb a:hover{{text-decoration:underline}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:9px 8px;border-bottom:1px solid #eee;text-align:left}}
th{{color:#6b7280;font-weight:500;background:#fafbfc}}
a{{color:#1f6feb;text-decoration:none}}a:hover{{text-decoration:underline}}
tr.dir td{{background:#fbfcfe}}
.foot{{color:#9ca3af;font-size:12px;margin-top:18px}}
</style></head><body>
<h2>共享文件</h2>
<div class="crumb">{crumb_html}</div>
<table>
<tr><th>名称</th><th style="width:110px">大小</th><th style="width:160px">修改时间</th></tr>
{"".join(rows)}
</table>
{tip}
<p class="foot">共 {len(dirs)} 个文件夹 · {len(files)} 个文件</p>
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 文件下载（流式 + 断点续传） ----------
    def _send_file(self, parts):
        if not parts:
            self._send_error_page(400, "缺少文件名")
            return
        target = self._resolve(parts)
        if target is None or not os.path.isfile(target):
            log("err", f"下载失败·文件不存在: {'/'.join(parts)} ← {self._client()}")
            self._send_error_page(404, "文件不存在")
            return

        name = parts[-1]
        size = os.path.getsize(target)
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        is_range = False

        if range_header and range_header.startswith("bytes="):
            try:
                spec = range_header[len("bytes="):].split(",")[0].strip()
                a, _, b = spec.partition("-")
                if a == "":
                    start = max(0, size - int(b)); end = size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                is_range = True
            except Exception:
                start, end = 0, size - 1
                is_range = False

        length = end - start + 1
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        self.send_response(206 if is_range else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        if is_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        rel_disp = "/".join(parts)
        tag = f"{rel_disp} ({human_size(length)}" + (
            f", 断点 {human_size(start)}起" if is_range else "") + ")"
        log("info", f"开始下载: {tag} ← {self._client()}")

        sent = 0
        t0 = time.time()
        try:
            with open(target, "rb") as fp:
                fp.seek(start)
                remain = length
                while remain > 0:
                    chunk = fp.read(min(CHUNK_SIZE, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remain -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            log("warn", f"下载中断: {rel_disp} 已传 {human_size(sent)}/{human_size(length)} ← {self._client()}")
            return
        except Exception as e:
            log("err", f"下载异常: {rel_disp} {e} ← {self._client()}")
            return

        cost = max(time.time() - t0, 0.001)
        speed = sent / cost
        log("ok", f"下载完成: {rel_disp} {human_size(sent)} 用时 {cost:.1f}s ({human_size(int(speed))}/s) ← {self._client()}")

    # ---------- 错误页 ----------
    def _send_error_page(self, code, msg):
        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{code}</title></head><body style="font-family:system-ui;max-width:600px;margin:80px auto;text-align:center;color:#374151">
<h2>{code}</h2><p>{msg}</p></body></html>"""
        body = html.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass


class ThreadingHTTPServerReuse(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


# ================== 服务控制 ==================
class ShareServer:
    def __init__(self, share_dir, bind_ip, port):
        self.share_dir = share_dir
        self.bind_ip = bind_ip
        self.port = port
        self.httpd = None
        self.thread = None
        self.running = False

    def start(self):
        ShareHandler.share_dir = self.share_dir
        self.httpd = ThreadingHTTPServerReuse((self.bind_ip, self.port), ShareHandler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            daemon=True,
        )
        self.thread.start()
        self.running = True
        log("ok", f"服务已启动: http://{self.bind_ip}:{self.port}/")
        if self.bind_ip == "0.0.0.0":
            log("info", "绑定网卡: 0.0.0.0（所有网卡均可访问）")
        else:
            log("info", f"绑定网卡: {self.bind_ip}（仅该地址可访问）")
        log("info", f"共享根目录: {self.share_dir}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.running = False
        log("warn", "服务已停止")


# ================== GUI ==================
class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.root.geometry("1000x720")
        self.root.minsize(840, 580)

        self.share_dir = os.path.join(BASE_DIR, "shared")
        os.makedirs(self.share_dir, exist_ok=True)

        self.server = None
        self.log_lines = 0
        self.active_bind_ip = None   # 当前实际绑定地址
        self.active_port = None      # 当前实际监听端口

        self._build_ui()
        self._refresh_files()
        self._poll_logs()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        log("info", f"程序目录: {BASE_DIR}")
        log("info", f"共享目录: {self.share_dir}")
        log("info", f"首选绑定: {FIXED_IP}:{FIXED_PORT}")

        # 启动后异步校验
        self.root.after(300, self._startup_ip_check)

    # ---------- 界面 ----------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")

        ttk.Label(top, text="共享目录:").pack(side="left")
        self.dir_var = tk.StringVar(value=self.share_dir)
        ent = ttk.Entry(top, textvariable=self.dir_var, state="readonly", width=46)
        ent.pack(side="left", padx=(6, 6), fill="x", expand=True)
        ttk.Button(top, text="选择", width=7, command=self._choose_dir).pack(side="left")
        ttk.Button(top, text="打开", width=7, command=self._open_dir).pack(side="left", padx=(4, 0))

        bar = ttk.Frame(self.root, padding=(12, 0))
        bar.pack(fill="x")

        ttk.Label(bar, text="首选绑定:").pack(side="left")
        ttk.Label(bar, text=f"{FIXED_IP}:{FIXED_PORT}",
                  foreground="#1f6feb", font=("微软雅黑", 10, "bold")).pack(side="left", padx=(4, 10))

        ttk.Button(bar, text="查看本机地址", width=12,
                   command=self._show_local_ips).pack(side="left", padx=(0, 10))

        self.btn_toggle = ttk.Button(bar, text="启动服务", command=self._toggle)
        self.btn_toggle.pack(side="left")

        self.status_var = tk.StringVar(value="● 未启动")
        self.status_lbl = ttk.Label(bar, textvariable=self.status_var, foreground="#9ca3af")
        self.status_lbl.pack(side="left", padx=14)

        self.addr_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.addr_var, foreground="#16a34a").pack(side="left")

        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=12, pady=(10, 12))

        # ---- 文件树 ----
        f1 = ttk.LabelFrame(paned, text="共享文件夹", padding=6)
        paned.add(f1, weight=3)

        cols = ("name", "size", "mtime")
        self.tree = ttk.Treeview(f1, columns=cols, show="tree headings", height=10)
        self.tree.heading("#0", text="名称")
        self.tree.heading("name", text="路径")
        self.tree.heading("size", text="大小")
        self.tree.heading("mtime", text="修改时间")
        self.tree.column("#0", width=280, anchor="w")
        self.tree.column("name", width=260, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("mtime", width=150, anchor="center")

        vsb = ttk.Scrollbar(f1, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(f1, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        f1.rowconfigure(0, weight=1)
        f1.columnconfigure(0, weight=1)

        ops = ttk.Frame(f1)
        ops.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        ttk.Button(ops, text="添加文件", width=11, command=self._import_file).pack(pady=(0, 6))
        ttk.Button(ops, text="添加文件夹", width=11, command=self._import_folder).pack(pady=(0, 6))
        ttk.Button(ops, text="刷新", width=11, command=self._refresh_files).pack(pady=(0, 6))
        ttk.Button(ops, text="移除", width=11, command=self._remove_file).pack(pady=(0, 6))
        ttk.Button(ops, text="复制地址", width=11, command=self._copy_addr).pack()

        self.tree.bind("<Double-1>", self._on_tree_dclick)

        # ---- 日志 ----
        f2 = ttk.LabelFrame(paned, text="实时日志", padding=6)
        paned.add(f2, weight=2)

        inner = ttk.Frame(f2)
        inner.pack(fill="both", expand=True)

        self.log_text = tk.Text(inner, wrap="word", height=12, state="disabled",
                                bg="#0d1117", fg="#c9d1d9", insertbackground="#fff",
                                font=("Consolas", 9), relief="flat", padx=8, pady=6)
        lsb = ttk.Scrollbar(inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        self.log_text.tag_config("time", foreground="#6e7681")
        self.log_text.tag_config("info", foreground="#58a6ff")
        self.log_text.tag_config("ok", foreground="#3fb950")
        self.log_text.tag_config("warn", foreground="#d29922")
        self.log_text.tag_config("err", foreground="#f85149")

        lb = ttk.Frame(f2)
        lb.pack(fill="x", pady=(6, 0))
        ttk.Button(lb, text="清空日志", width=10, command=self._clear_log).pack(side="left")
        ttk.Button(lb, text="导出日志", width=10, command=self._export_log).pack(side="left", padx=6)
        self.stat_var = tk.StringVar(value="")
        ttk.Label(lb, textvariable=self.stat_var, foreground="#6b7280").pack(side="right")

    # ---------- IP 相关 ----------
    def _startup_ip_check(self):
        if is_wildcard_ip(FIXED_IP):
            log("ok", f"当前绑定地址 {FIXED_IP or '0.0.0.0'} 为通配模式，监听所有网卡，无需 IP 校验")
            _, ips = check_local_ip("")
            log("info", f"本机 IPv4 地址: {', '.join(ips)}")
            return

        ok, ips = check_local_ip(FIXED_IP)
        if ok:
            log("ok", f"本机已具备首选地址 {FIXED_IP}，可直接启动服务")
            return

        log("warn", f"本机未检测到 {FIXED_IP}")
        log("info", f"当前本机 IPv4 地址: {', '.join(ips)}")

        radmin_like = [ip for ip in ips if ip.startswith("26.")]
        if radmin_like:
            log("warn", f"检测到 26.x 段地址 {radmin_like[0]}，"
                        f"若这就是 Radmin 分配给你的地址，可点「启动服务」后手动指定")
        else:
            log("warn", "未发现 26.x 段地址，Radmin VPN 可能未连接或虚拟网卡被禁用")

    def _show_local_ips(self):
        ok, ips = check_local_ip(FIXED_IP)
        wildcard = is_wildcard_ip(FIXED_IP)

        if wildcard:
            head = f"当前绑定地址 {FIXED_IP or '0.0.0.0'}：通配模式（监听所有网卡）"
        else:
            head = f"首选地址 {FIXED_IP}：{'可用' if ok else '不可用'}"

        lines = []
        for ip in ips:
            mark = ""
            if not wildcard and ip == FIXED_IP:
                mark = "  ← 首选地址"
            lines.append(f"  {ip}{mark}")

        # 提示对外访问地址
        external = [x for x in ips if not x.startswith("127.") and not x.startswith("169.254.")]
        tip_addr = external[0] if external else "127.0.0.1"

        messagebox.showinfo(
            "本机 IPv4 地址",
            f"{head}\n\n"
            f"本机全部 IPv4:\n" + "\n".join(lines) + "\n\n"
            f"客户端可访问地址示例：\n"
            f"  http://{tip_addr}:{self.active_port or FIXED_PORT}/\n\n"
            f"提示：Radmin VPN 的地址通常以 26. 开头。"
        )

    # ---------- 文件树 ----------
    def _refresh_files(self):
        for i in self.tree.get_children(""):
            self.tree.delete(i)
        try:
            dirs, files = self._scan(self.share_dir)
            for name in dirs:
                node = self.tree.insert("", "end", text=f"📁 {name}",
                                        values=("", "—", ""), open=False)
                self.tree.insert(node, "end", text="加载中…")
            for name, size, mtime in files:
                self.tree.insert("", "end", text=f"📄 {name}",
                                 values=(name, human_size(size), mtime))
            self.stat_var.set(f"{len(dirs)} 个文件夹 · {len(files)} 个文件")
        except Exception as e:
            log("err", f"刷新列表失败: {e}")

    def _scan(self, path):
        dirs, files = [], []
        with os.scandir(path) as it:
            for entry in it:
                try:
                    st = entry.stat()
                except OSError:
                    continue
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                if entry.is_dir():
                    dirs.append(entry.name)
                else:
                    files.append((entry.name, st.st_size, mtime))
        dirs.sort(key=str.lower)
        files.sort(key=lambda x: x[0].lower())
        return dirs, files

    def _on_tree_dclick(self, event):
        node = self.tree.identify_row(event.y)
        if node:
            self._load_children(node)

    def _load_children(self, node):
        children = self.tree.get_children(node)
        if len(children) == 1 and self.tree.item(children[0], "text") == "加载中…":
            self.tree.delete(children[0])
            rel = self._node_relpath(node)
            full = os.path.join(self.share_dir, rel) if rel else self.share_dir
            try:
                dirs, files = self._scan(full)
                for name in dirs:
                    sub = self.tree.insert(node, "end", text=f"📁 {name}",
                                           values=("", "—", ""), open=False)
                    self.tree.insert(sub, "end", text="加载中…")
                for name, size, mtime in files:
                    self.tree.insert(node, "end", text=f"📄 {name}",
                                     values=(os.path.join(rel, name), human_size(size), mtime))
            except Exception as e:
                log("err", f"读取子目录失败: {e}")

    def _node_relpath(self, node):
        parts = []
        cur = node
        while cur:
            txt = self.tree.item(cur, "text")
            name = txt.replace("📁 ", "").replace("📄 ", "").strip()
            if name:
                parts.append(name)
            cur = self.tree.parent(cur)
        parts.reverse()
        return os.path.join(*parts) if parts else ""

    # ---------- 目录操作 ----------
    def _choose_dir(self):
        d = filedialog.askdirectory(title="选择共享目录", initialdir=self.share_dir)
        if not d:
            return
        if self.server and self.server.running:
            log("warn", "服务运行中，目录变更将在重启服务后生效")
        self.share_dir = d
        self.dir_var.set(d)
        self._refresh_files()
        log("info", f"共享目录已设为: {d}")

    def _open_dir(self):
        try:
            os.startfile(self.share_dir)
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", self.share_dir])
        except Exception as e:
            log("err", f"打开目录失败: {e}")

    def _import_file(self):
        paths = filedialog.askopenfilenames(title="选择要共享的文件")
        if not paths:
            return
        for p in paths:
            dst = os.path.join(self.share_dir, os.path.basename(p))
            if os.path.abspath(p) == os.path.abspath(dst):
                continue
            try:
                shutil.copy2(p, dst)
                log("ok", f"已添加文件: {os.path.basename(p)} ({human_size(os.path.getsize(dst))})")
            except Exception as e:
                log("err", f"添加失败 {os.path.basename(p)}: {e}")
        self._refresh_files()

    def _import_folder(self):
        src = filedialog.askdirectory(title="选择要添加的文件夹")
        if not src:
            return
        dst = os.path.join(self.share_dir, os.path.basename(src.rstrip("\\/")))
        if os.path.abspath(src) == os.path.abspath(dst):
            log("warn", "源文件夹与目标相同，已跳过")
            return
        if os.path.exists(dst):
            if not messagebox.askyesno("已存在", f"文件夹 {os.path.basename(src)} 已存在，是否合并？"):
                return
        log("info", f"开始复制文件夹: {src} → {dst}")
        try:
            n = self._copy_tree(src, dst)
            log("ok", f"文件夹添加完成，共 {n} 个文件")
        except Exception as e:
            log("err", f"添加文件夹失败: {e}")
        self._refresh_files()

    def _copy_tree(self, src, dst):
        count = 0
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_dir = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target_dir, exist_ok=True)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(target_dir, f)
                try:
                    shutil.copy2(s, d)
                    count += 1
                except Exception as e:
                    log("err", f"复制失败 {s}: {e}")
        return count

    def _remove_file(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中要移除的项目")
            return

        targets = []
        for node in sel:
            txt = self.tree.item(node, "text")
            if txt.startswith("📁"):
                targets.append(os.path.join(self.share_dir, self._node_relpath(node)))
            else:
                vals = self.tree.item(node, "values")
                rel = vals[0] if vals and vals[0] else txt.replace("📄 ", "")
                targets.append(os.path.join(self.share_dir, rel))

        names = [os.path.basename(t) for t in targets]
        if not messagebox.askyesno("确认", "从共享目录删除以下项目？\n\n" + "\n".join(names)):
            return

        for t in targets:
            try:
                if os.path.isdir(t):
                    shutil.rmtree(t)
                    log("warn", f"已删除文件夹: {os.path.basename(t)}")
                elif os.path.isfile(t):
                    os.remove(t)
                    log("warn", f"已删除文件: {os.path.basename(t)}")
            except Exception as e:
                log("err", f"删除失败 {t}: {e}")
        self._refresh_files()

    def _copy_addr(self):
        if not (self.server and self.server.running):
            messagebox.showinfo("提示", "请先启动服务")
            return

        port = self.active_port or FIXED_PORT
        ip = self.active_bind_ip or FIXED_IP

        if is_wildcard_ip(ip):
            # 绑 0.0.0.0 时，给用户列一个可访问的对外地址
            _, ips = check_local_ip("")
            candidates = [x for x in ips
                          if not x.startswith("127.") and not x.startswith("169.254.")]
            radmin = [x for x in candidates if x.startswith("26.")]
            host = (radmin or candidates or ["127.0.0.1"])[0]
            log("info", f"当前为通配绑定，已选择 {host} 作为对外展示地址")
        else:
            host = ip

        addr = f"http://{host}:{port}/"
        self.root.clipboard_clear()
        self.root.clipboard_append(addr)
        log("ok", f"已复制地址到剪贴板: {addr}")

    # ---------- 服务控制 ----------
    def _toggle(self):
        if self.server and self.server.running:
            self.server.stop()
            self.server = None
            self.active_bind_ip = None
            self.active_port = None
            self.btn_toggle.config(text="启动服务")
            self.status_var.set("● 已停止")
            self.status_lbl.config(foreground="#9ca3af")
            self.addr_var.set("")
            return

        bind_ip = self._decide_bind_ip()
        if bind_ip is None:
            return

        # 端口预检
        ok, err = probe_port(bind_ip, FIXED_PORT)
        if not ok:
            log("err", f"端口预检失败: {err}")

            alt = self._find_free_port(bind_ip)
            if alt:
                if messagebox.askyesno(
                        "端口不可用",
                        f"{err}\n\n是否改用端口 {alt} 启动？"):
                    port = alt
                else:
                    return
            else:
                messagebox.showerror(
                    "端口不可用",
                    f"{err}\n\n解决办法：\n"
                    f"1. 以管理员身份运行本程序\n"
                    f"2. 管理员 CMD: net stop winnat && net start winnat\n"
                    f"3. 查看保留段: netsh int ipv4 show excludedportrange protocol=tcp"
                )
                return
        else:
            port = FIXED_PORT

        try:
            self.server = ShareServer(self.share_dir, bind_ip, port)
            self.server.start()
        except PermissionError:
            log("err", f"端口 {port} 权限不足")
            messagebox.showerror(
                "启动失败 · 权限不足",
                f"端口 {port} 在 Windows 上受保护，普通用户无法绑定。\n\n"
                f"请以管理员身份运行，或改用其他端口。"
            )
            self.server = None
            return
        except OSError as e:
            log("err", f"启动失败: {e}")
            messagebox.showerror(
                "启动失败",
                f"无法绑定 {bind_ip}:{port}\n\n{e}\n\n"
                f"常见原因：\n"
                f"• 端口被其他程序占用\n"
                f"• Radmin VPN 未连接\n"
                f"• 防火墙拦截"
            )
            self.server = None
            return

        self.active_bind_ip = bind_ip
        self.active_port = port
        self.btn_toggle.config(text="停止服务")
        self.status_var.set("● 运行中")
        self.status_lbl.config(foreground="#16a34a")
        self.addr_var.set(f"监听 {bind_ip}:{port}")

    def _decide_bind_ip(self):
        """确定本次要绑定的地址；返回 None 表示取消"""
        # 通配地址：直接放行，不做网卡校验
        if is_wildcard_ip(FIXED_IP):
            log("info", f"使用通配绑定 {FIXED_IP or '0.0.0.0'}（监听所有网卡）")
            return FIXED_IP or "0.0.0.0"

        ok, ips = check_local_ip(FIXED_IP)
        if ok:
            return FIXED_IP

        log("err", f"本机不存在 {FIXED_IP}")
        log("info", f"当前本机 IPv4: {', '.join(ips)}")

        ans = messagebox.askyesnocancel(
            "地址不可用",
            f"本机未检测到 {FIXED_IP}。\n\n"
            f"当前可用 IPv4:\n" + "\n".join(f"  {x}" for x in ips) + "\n\n"
            f"【是】绑定 0.0.0.0（所有网卡都能访问）\n"
            f"【否】手动输入一个本机地址\n"
            f"【取消】放弃启动"
        )
        if ans is None:
            return None

        if ans is True:
            log("warn", "降级为绑定 0.0.0.0，所有网卡均可访问")
            return "0.0.0.0"

        default_ip = ips[0] if ips else ""
        for ip in ips:
            if ip.startswith("26."):
                default_ip = ip
                break

        manual = simpledialog.askstring(
            "指定绑定地址",
            "请输入本机已有的 IPv4 地址：",
            parent=self.root, initialvalue=default_ip)
        if not manual:
            return None
        manual = manual.strip()
        if not check_local_ip(manual)[0]:
            messagebox.showerror("地址无效", f"{manual} 不是本机地址")
            return None
        log("info", f"改用用户指定地址: {manual}")
        return manual

    @staticmethod
    def _find_free_port(bind_ip, start=8765, limit=30):
        """从 start 开始找一个可绑定的端口"""
        for p in range(start, start + limit):
            ok, _ = probe_port(bind_ip, p)
            if ok:
                return p
        return None

    # ---------- 日志轮询 ----------
    def _poll_logs(self):
        added = 0
        while True:
            try:
                rec = LOG.q.get_nowait()
            except queue.Empty:
                break
            self.log_text.config(state="normal")
            self.log_text.insert("end", f"{rec['time']} ", "time")
            self.log_text.insert("end", f"[{rec['level']}] ", rec["level"])
            self.log_text.insert("end", rec["msg"] + "\n")
            self.log_text.config(state="disabled")
            self.log_text.see("end")
            self.log_lines += 1
            added += 1
            if added > 300:
                break
        if self.log_lines > 3000:
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "500.0")
            self.log_text.config(state="disabled")
            self.log_lines -= 499
        self.root.after(150, self._poll_logs)

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.log_lines = 0

    def _export_log(self):
        p = filedialog.asksaveasfilename(
            title="导出日志", defaultextension=".txt",
            initialfile=f"share_log_{datetime.now():%Y%m%d_%H%M%S}.txt",
            filetypes=[("文本文件", "*.txt")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                for r in LOG.all_logs:
                    f.write(f"{r['time']} [{r['level']}] {r['msg']}\n")
            messagebox.showinfo("完成", f"日志已导出到:\n{p}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _on_close(self):
        if self.server and self.server.running:
            if not messagebox.askyesno("退出", "服务正在运行，确定退出？"):
                return
            self.server.stop()
        self.root.destroy()


# ================== 入口 ==================
def main():
    try:
        root = tk.Tk()
        try:
            style = ttk.Style()
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except Exception:
            pass
        App(root)
        root.mainloop()
    except Exception:
        crash_path = os.path.join(BASE_DIR, "crash.log")
        try:
            with open(crash_path, "w", encoding="utf-8") as f:
                f.write(f"时间: {datetime.now()}\n")
                f.write(f"程序目录: {BASE_DIR}\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"frozen: {getattr(sys, 'frozen', False)}\n\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        try:
            import tkinter.messagebox as mb
            mb.showerror("程序异常", f"发生未处理异常，详情已写入:\n{crash_path}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
