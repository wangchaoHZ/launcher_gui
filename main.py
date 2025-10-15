import os
import sys
import time
import json
import socket
import threading
import subprocess
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# 可选 HTTP 健康检查
try:
    import requests
except ImportError:
    requests = None  # 仅在 wait.type = http 时需要

CONFIG_FILE = "services.json"

# 日志队列
LOG_QUEUE = queue.Queue()
STOP_EVENT = threading.Event()

STATUS_IDLE = "Idle"
STATUS_STARTING = "Starting"
STATUS_RUNNING = "Running"
STATUS_FAILED = "Failed"
STATUS_STOPPED = "Stopped"
STATUS_EXITED = "Exited"

DEFAULT_START_INTERVAL = 5


def script_dir():
    return os.path.dirname(
        os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
    )


def load_config(path: str):
    """
    读取并校验配置文件
    返回: (start_interval_seconds, services list)
    """
    if not os.path.isfile(path):
        # 自动生成模板
        template = {
            "start_interval_seconds": 5,
            "services": [
                {
                    "name": "ExampleService",
                    "cmd": ["C:/path/to/app.exe", "--flag"],
                    "cwd": "C:/path/to",
                    "wait": {"type": "none"},
                    "auto_restart": False,
                    "max_restarts": 0,
                    "restart_backoff": 2,
                    "restart_backoff_factor": 1.5,
                    "required_files": []
                }
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2, ensure_ascii=False)
        raise FileNotFoundError(
            f"未找到配置文件，已自动生成模板: {path} 请修改后重新启动。"
        )

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("配置根对象必须是 JSON 对象 {}")

    services = data.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("配置中 'services' 必须是非空数组")

    # 去重校验
    names = set()
    for svc in services:
        if not isinstance(svc, dict):
            raise ValueError("services 数组内每一项必须是对象")
        name = svc.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("每个服务必须具有字符串 'name'")
        if name in names:
            raise ValueError(f"服务名称重复: {name}")
        names.add(name)

        cmd = svc.get("cmd")
        if not cmd or not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            raise ValueError(f"{name}: 'cmd' 必须是字符串数组，至少包含一个元素（可执行文件路径）")

        wait_cfg = svc.get("wait", {"type": "none"})
        if not isinstance(wait_cfg, dict):
            raise ValueError(f"{name}: 'wait' 必须是对象")
        wtype = wait_cfg.get("type", "none")
        if wtype not in ("none", "port", "http"):
            raise ValueError(f"{name}: wait.type 只能是 none|port|http")

        if wtype == "port":
            if "value" not in wait_cfg or not isinstance(wait_cfg["value"], int):
                raise ValueError(f"{name}: wait.type=port 时必须提供整数 value 端口号")
        if wtype == "http":
            if "value" not in wait_cfg or not isinstance(wait_cfg["value"], str):
                raise ValueError(f"{name}: wait.type=http 时必须提供字符串 value URL")

        # 默认值填充
        svc.setdefault("auto_restart", False)
        svc.setdefault("max_restarts", 0)
        svc.setdefault("restart_backoff", 2)
        svc.setdefault("restart_backoff_factor", 1.5)
        svc.setdefault("required_files", [])
        if not isinstance(svc["required_files"], list):
            raise ValueError(f"{name}: required_files 必须是数组")

    start_interval = data.get("start_interval_seconds", DEFAULT_START_INTERVAL)
    if not isinstance(start_interval, int) or start_interval < 0:
        raise ValueError("'start_interval_seconds' 必须是非负整数")

    return start_interval, services


def utc_ts():
    return time.strftime("%H:%M:%S")


class ServiceRuntime:
    def __init__(self, spec):
        self.spec = spec
        self.name = spec["name"]
        self.cmd = spec["cmd"]
        self.cwd = spec.get("cwd") or script_dir()
        self.wait_cfg = spec.get("wait", {"type": "none"})
        self.auto_restart = spec.get("auto_restart", False)
        self.max_restarts = spec.get("max_restarts", 0)
        self.restart_backoff = spec.get("restart_backoff", 2)
        self.restart_backoff_factor = spec.get("restart_backoff_factor", 1.5)
        self.required_files = spec.get("required_files", [])

        self.proc = None
        self.status = STATUS_IDLE
        self.restarts = 0
        self._stdout_thread = None
        self._lock = threading.Lock()
        self._stop_requested = False

    def log(self, msg: str):
        LOG_QUEUE.put(f"[{utc_ts()}][{self.name}] {msg}")

    def start(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.log("已在运行，忽略启动。")
                return
            self._stop_requested = False
            self.status = STATUS_STARTING

        # 必要文件检查（若你已有就保留）
        missing = []
        for rf in self.required_files:
            if not os.path.isfile(os.path.join(self.cwd, rf)):
                missing.append(rf)
        if missing:
            with self._lock:
                self.status = STATUS_FAILED
            self.log(f"缺少必要文件: {', '.join(missing)}，取消启动。")
            return

        self.log(f"启动命令: {self.cmd}")

        try:
            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                # 隐藏控制台窗口
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                # 双保险：有些情况下再加 STARTUPINFO
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                # startupinfo.wShowWindow = 0  # 可显式设为 SW_HIDE

            self.proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            self.log(f"已启动 PID={self.proc.pid}")
        except FileNotFoundError:
            with self._lock:
                self.status = STATUS_FAILED
            self.log("执行文件未找到。")
            self._maybe_schedule_restart()
            return
        except Exception as e:
            with self._lock:
                self.status = STATUS_FAILED
            self.log(f"启动异常: {e}")
            self._maybe_schedule_restart()
            return

        self._stdout_thread = threading.Thread(
            target=self._read_stdout_loop, daemon=True
        )
        self._stdout_thread.start()

        ok = self._wait_health()
        with self._lock:
            if ok:
                self.status = STATUS_RUNNING
                self.log("健康检查通过，运行中。")
            else:
                if self.status == STATUS_STARTING:
                    self.status = STATUS_FAILED
                self.log("健康检查失败或进程提前退出，终止。")
                self._terminate_internal(force=True)

        if self.status == STATUS_FAILED:
            self._maybe_schedule_restart()

    def _read_stdout_loop(self):
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            if line:
                self.log(line.rstrip("\r\n"))
        code = self.proc.poll()
        if code is None:
            code = self.proc.wait()
        self.log(f"进程退出 code={code}")
        with self._lock:
            if self.status not in (STATUS_STOPPED, STATUS_FAILED):
                self.status = STATUS_EXITED
        self._maybe_schedule_restart()

    def _wait_health(self):
        wtype = self.wait_cfg.get("type", "none")
        if wtype == "none":
            return True
        timeout = int(self.wait_cfg.get("timeout", 60))
        start = time.time()
        if wtype == "port":
            port = int(self.wait_cfg.get("value"))
            self.log(f"等待端口 {port} (<= {timeout}s)")
            while time.time() - start < timeout:
                if self._stop_requested:
                    return False
                if self.proc and self.proc.poll() is not None:
                    self.log(f"进程提前退出 code={self.proc.returncode}，停止等待端口。")
                    return False
                if self._port_open(port):
                    return True
                time.sleep(0.5)
            return False
        if wtype == "http":
            if requests is None:
                self.log("缺少 requests 库，无法进行 HTTP 健康检查。")
                return False
            url = self.wait_cfg.get("value")
            self.log(f"等待 HTTP {url} (<= {timeout}s)")
            while time.time() - start < timeout:
                if self._stop_requested:
                    return False
                if self.proc and self.proc.poll() is not None:
                    self.log(f"进程提前退出 code={self.proc.returncode}，停止等待 HTTP。")
                    return False
                try:
                    r = requests.get(url, timeout=2)
                    if r.status_code < 400:
                        return True
                except Exception:
                    pass
                time.sleep(0.5)
            return False
        self.log(f"未知 wait.type={wtype}，跳过。")
        return True

    def _port_open(self, port: int) -> bool:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except Exception:
            return False

    def stop(self, force=True):
        with self._lock:
            self._stop_requested = True
        self._terminate_internal(force=force)
        with self._lock:
            if self.status not in (STATUS_FAILED, STATUS_EXITED):
                self.status = STATUS_STOPPED
        self.log("已请求停止。")

    def _terminate_internal(self, force=False):
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            for _ in range(50):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is None and force:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _maybe_schedule_restart(self):
        if not self.auto_restart or self._stop_requested or STOP_EVENT.is_set():
            return
        if self.max_restarts >= 0 and self.restarts >= self.max_restarts:
            self.log("已达到最大重启次数，不再重启。")
            return
        self.restarts += 1
        delay = self.restart_backoff * (self.restart_backoff_factor ** (self.restarts - 1))
        self.log(f"计划第 {self.restarts} 次重启，{delay:.1f}s 后执行。")
        threading.Thread(target=self._delayed_restart, args=(delay,), daemon=True).start()

    def _delayed_restart(self, delay):
        t0 = time.time()
        while time.time() - t0 < delay:
            if self._stop_requested or STOP_EVENT.is_set():
                return
            time.sleep(0.2)
        if not self._stop_requested and not STOP_EVENT.is_set():
            self.start()


class LauncherGUI:
    def __init__(self, root, config_path):
        self.root = root
        self.config_path = config_path
        self.root.title("系统服务启动器")

        self.start_interval_seconds, services_specs = self._safe_load_config(initial=True)
        self.services = [ServiceRuntime(s) for s in services_specs]
        self.service_map = {s.name: s for s in self.services}

        self._build_widgets()
        self._populate_tree()
        self._schedule_status_refresh()
        self._schedule_log_drain()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _safe_load_config(self, initial=False):
        try:
            return load_config(os.path.join(script_dir(), self.config_path))
        except Exception as e:
            if initial:
                messagebox.showerror("配置加载失败", str(e))
                # 初始加载失败仍然提供空界面
                return DEFAULT_START_INTERVAL, []
            else:
                messagebox.showerror("重新加载失败", str(e))
                raise

    def _build_widgets(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)

        ttk.Button(top, text="启动全部", command=self.start_all).pack(side="left", padx=4)
        ttk.Button(top, text="停止全部", command=self.stop_all).pack(side="left", padx=4)
        ttk.Button(top, text="启动选中", command=self.start_selected).pack(side="left", padx=4)
        ttk.Button(top, text="停止选中", command=self.stop_selected).pack(side="left", padx=4)
        ttk.Button(top, text="重新加载配置", command=self.reload_config).pack(side="left", padx=4)
        ttk.Button(top, text="导出日志", command=self.export_logs).pack(side="left", padx=4)

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=False, padx=8)

        columns = ("name", "status", "pid", "restarts")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", height=6)
        self.tree.heading("name", text="服务")
        self.tree.heading("status", text="状态")
        self.tree.heading("pid", text="PID")
        self.tree.heading("restarts", text="重启次数")
        self.tree.column("name", width=150)
        self.tree.column("status", width=90)
        self.tree.column("pid", width=70, anchor="center")
        self.tree.column("restarts", width=80, anchor="center")
        self.tree.pack(fill="x", pady=4)

        log_frame = ttk.LabelFrame(self.root, text="日志")
        log_frame.pack(fill="both", expand=True, padx=8, pady=6)

        self.txt = tk.Text(
            log_frame, height=18, wrap="none", bg="#111111", fg="#DDDDDD", insertbackground="#FFFFFF"
        )
        self.txt.pack(fill="both", expand=True, side="left")

        # 预定义高亮
        self.txt.tag_config("err", foreground="#ff5555")
        self.txt.tag_config("warn", foreground="#ffaa00")

        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w")
        status_bar.pack(fill="x", padx=6, pady=(0, 6))

    def _populate_tree(self):
        # 清空旧
        for item in self.tree.get_children():
            self.tree.delete(item)
        for s in self.services:
            self.tree.insert("", "end", iid=s.name, values=(s.name, s.status, "-", 0))

    # ------------------- 操作按钮 -------------------

    def start_all(self):
        if not self.services:
            messagebox.showinfo("提示", "当前没有加载任何服务。")
            return
        self.status_var.set("按顺序启动全部中...")
        threading.Thread(target=self._start_all_thread, daemon=True).start()

    def _start_all_thread(self):
        for i, svc in enumerate(self.services):
            if STOP_EVENT.is_set():
                return
            svc.start()
            if i < len(self.services) - 1 and self.start_interval_seconds > 0:
                svc.log(f"等待 {self.start_interval_seconds} 秒再启动下一个服务...")
                for _ in range(self.start_interval_seconds):
                    if STOP_EVENT.is_set():
                        return
                    time.sleep(1)
        self.status_var.set("全部启动流程结束。")

    def stop_all(self):
        self.status_var.set("停止全部中...")
        for svc in self.services:
            svc.stop(force=True)
        self.status_var.set("全部停止指令已发送。")

    def start_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请选择至少一个服务。")
            return
        for iid in sel:
            svc = self.service_map.get(iid)
            if svc:
                svc.start()

    def stop_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请选择至少一个服务。")
            return
        for iid in sel:
            svc = self.service_map.get(iid)
            if svc:
                svc.stop(force=True)

    def export_logs(self):
        content = self.txt.get("1.0", "end").rstrip()
        if not content:
            messagebox.showinfo("提示", "当前没有日志。")
            return
        fname = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".log",
            filetypes=[("Log Files", "*.log"), ("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if fname:
            try:
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(content)
                messagebox.showinfo("成功", f"已保存: {fname}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {e}")

    def reload_config(self):
        # 不允许在有运行进程时热重载（避免状态对象失配）
        running = [s for s in self.services if s.proc and s.proc.poll() is None]
        if running:
            messagebox.showwarning("禁止重载", "请先停止所有正在运行的服务，再重载配置。")
            return
        try:
            start_interval, specs = self._safe_load_config(initial=False)
        except Exception:
            return
        self.start_interval_seconds = start_interval
        self.services = [ServiceRuntime(s) for s in specs]
        self.service_map = {s.name: s for s in self.services}
        self._populate_tree()
        self.log_system(f"配置已重新加载，服务数量: {len(self.services)}; 启动间隔: {self.start_interval_seconds}s")

    def log_system(self, msg):
        LOG_QUEUE.put(f"[{utc_ts()}][SYSTEM] {msg}")

    # ------------------- 状态刷新 / 日志刷新 -------------------

    def _schedule_status_refresh(self):
        self._refresh_status_table()
        self.root.after(1000, self._schedule_status_refresh)

    def _refresh_status_table(self):
        for svc in self.services:
            pid = "-"
            if svc.proc and svc.proc.poll() is None:
                pid = str(svc.proc.pid)
            if self.tree.exists(svc.name):
                self.tree.set(svc.name, "status", svc.status)
                self.tree.set(svc.name, "pid", pid)
                self.tree.set(svc.name, "restarts", svc.restarts)

    def _schedule_log_drain(self):
        self._drain_logs()
        self.root.after(250, self._schedule_log_drain)

    def _drain_logs(self):
        updated = False
        while True:
            try:
                line = LOG_QUEUE.get_nowait()
            except queue.Empty:
                break
            updated = True
            self._append_log_line(line)
        if updated:
            self.txt.see("end")

    def _append_log_line(self, line: str):
        line = line.rstrip("\r\n")
        lower = line.lower()
        tag = None
        if any(k in lower for k in ("error", "failed", "缺少", "missing")):
            tag = "err"
        elif "warn" in lower:
            tag = "warn"
        if tag:
            self.txt.insert("end", line + "\n", tag)
        else:
            self.txt.insert("end", line + "\n")

    # ------------------- 关闭事件 -------------------

    def on_close(self):
        if messagebox.askokcancel("退出", "确认关闭并终止所有运行中的服务？"):
            STOP_EVENT.set()
            for svc in self.services:
                svc.stop(force=True)
            self.root.after(300, self.root.destroy)


def main():
    root = tk.Tk()
    app = LauncherGUI(root, CONFIG_FILE)
    root.mainloop()


if __name__ == "__main__":
    main()