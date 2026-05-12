#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Visual repair console for local Codex history visibility issues."""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import codex_history_keeper as keeper  # noqa: E402


APP_TITLE = "Codex History Repair"
WATCHER_SCRIPT = SCRIPT_DIR / "repair_after_codex_closes.ps1"
GUI_SCRIPT = Path(__file__).resolve()
REPAIR_LOG = Path.home() / ".codex_history_keeper" / "repair_after_close.log"


def strip_extended_path(path_text: str) -> str:
    return path_text[4:] if path_text.startswith("\\\\?\\") else path_text


def read_session_provider_counts(codex_home: Path) -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for path in keeper.session_files(codex_home):
        provider = "(missing)"
        for row in keeper.read_jsonl(path):
            payload = row.get("payload") or {}
            if row.get("type") == "session_meta" and isinstance(payload, dict):
                provider = str(payload.get("model_provider") or "(missing)")
                break
        counts[provider] += 1
    return counts


def read_state_summary(codex_home: Path) -> dict[str, object]:
    db_path = codex_home / "state_5.sqlite"
    summary: dict[str, object] = {
        "exists": db_path.exists(),
        "quick_check": "missing",
        "thread_count": 0,
        "provider_groups": [],
        "visible_projects": [],
        "mtime": "",
    }
    if not db_path.exists():
        return summary

    summary["mtime"] = dt.datetime.fromtimestamp(db_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        summary["quick_check"] = con.execute("pragma quick_check").fetchone()[0]
        summary["thread_count"] = con.execute("select count(*) from threads").fetchone()[0]
        summary["provider_groups"] = con.execute(
            "select model_provider, source, archived, has_user_event, count(*) "
            "from threads group by model_provider, source, archived, has_user_event "
            "order by count(*) desc"
        ).fetchall()
        summary["visible_projects"] = con.execute(
            "select cwd, count(*) from threads "
            "where archived=0 and has_user_event=1 "
            "group by cwd order by count(*) desc"
        ).fetchall()
    finally:
        con.close()
    return summary


def latest_log_tail(path: Path, lines: int = 18) -> str:
    if not path.exists():
        return "(no log yet)"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def write_repair_log(message: str) -> None:
    REPAIR_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with REPAIR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def create_gui_shortcut() -> Path:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    link_path = desktop / "Codex History Repair.lnk"
    target = keeper.pythonw_executable()
    keeper.create_shortcut(
        link_path=link_path,
        target_path=target,
        arguments=f'"{GUI_SCRIPT}"',
        working_directory=SCRIPT_DIR,
        description="Open the visual Codex history repair console.",
    )
    return link_path


class RepairGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.config_data = keeper.load_config()
        self.target_provider_var = tk.StringVar(value="current")
        self.diagnosis_running = False
        self.repair_running = False
        self.watcher_running = False
        self.export_running = False
        self._last_log_text = ""
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(860, 620)
        self.configure(bg="#f6f7f9")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._setup_style()
        self._build_ui()
        self.after(200, self.refresh_diagnostics)
        self.after(2500, self.refresh_log_loop)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"), background="#f6f7f9")
        style.configure("Hint.TLabel", foreground="#5f6673", background="#f6f7f9")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("TButton", padding=(10, 7))

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(18, 16, 18, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Codex 历史可视化修复工具", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="用于诊断并修复切换账号/API 后左侧项目显示“暂无对话”的本地索引问题，也可以备份可读对话。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        controls = ttk.Frame(self, padding=(18, 0, 18, 10))
        controls.grid(row=1, column=0, sticky="ew")
        for i in range(6):
            controls.columnconfigure(i, weight=1)
        ttk.Button(controls, text="刷新诊断", command=self.refresh_diagnostics).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(controls, text="关闭后自动修复", command=self.start_watcher).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(controls, text="立即修复", command=self.apply_now).grid(row=0, column=2, sticky="ew", padx=6)
        ttk.Button(controls, text="备份对话", command=self.export_vault).grid(row=0, column=3, sticky="ew", padx=6)
        ttk.Button(controls, text="查看备份", command=self.open_vault).grid(row=0, column=4, sticky="ew", padx=6)
        ttk.Button(controls, text="创建桌面图标", command=self.create_shortcut).grid(row=0, column=5, sticky="ew", padx=(6, 0))
        ttk.Label(controls, text="目标提供者").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.provider_combo = ttk.Combobox(controls, textvariable=self.target_provider_var, state="readonly")
        self.provider_combo.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(10, 0))
        ttk.Label(
            controls,
            text="current = 自动读取当前 Codex 配置；官方账号通常是 openai，API 模式通常是 my_codex/sub2api 等。",
            style="Hint.TLabel",
        ).grid(row=1, column=3, columnspan=3, sticky="w", padx=(6, 0), pady=(10, 0))

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 12))

        left = ttk.Frame(main, style="Card.TFrame", padding=12)
        right = ttk.Frame(main, style="Card.TFrame", padding=12)
        main.add(left, weight=2)
        main.add(right, weight=3)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="当前诊断", font=("Segoe UI", 11, "bold"), background="#ffffff").grid(row=0, column=0, sticky="w")
        self.diagnosis = tk.Text(left, height=22, wrap="word", relief="flat", bg="#ffffff", fg="#1f2328")
        self.diagnosis.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        ttk.Label(right, text="修复日志", font=("Segoe UI", 11, "bold"), background="#ffffff").grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(right, height=22, wrap="word", relief="flat", bg="#ffffff", fg="#1f2328")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        footer = ttk.Frame(self, padding=(18, 0, 18, 14))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(footer, textvariable=self.status_var, style="Hint.TLabel").grid(row=0, column=0, sticky="w")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def provider_mode(self) -> str:
        return self.target_provider_var.get().strip() or "current"

    def update_provider_options(self, values: list[str]) -> None:
        seen: list[str] = []
        for value in values:
            if value not in seen:
                seen.append(value)
        self.provider_combo.configure(values=seen)
        if self.target_provider_var.get() not in seen:
            self.target_provider_var.set("current")

    def refresh_diagnostics(self) -> None:
        if self.diagnosis_running:
            self.set_status("诊断正在后台进行...")
            return
        self.diagnosis_running = True
        self.set_status("正在后台诊断...")
        target_mode = self.provider_mode()

        def worker() -> None:
            try:
                text = self.build_diagnostics_text(target_mode)
                self.after(0, lambda: self.set_text(self.diagnosis, text))
                self.after(0, lambda: self.set_status("诊断已刷新"))
            except Exception as exc:
                trace = traceback.format_exc()
                message = str(exc)
                self.after(0, lambda: self.set_text(self.diagnosis, trace))
                self.after(0, lambda message=message: self.set_status(f"诊断失败: {message}"))
            finally:
                self.diagnosis_running = False

        threading.Thread(target=worker, daemon=True).start()

    def build_diagnostics_text(self, target_mode: str) -> str:
        try:
            codex_home = Path(self.config_data["codex_home"]).expanduser()
            provider_options = ["current", *keeper.known_provider_names(codex_home)]
            self.after(0, lambda: self.update_provider_options(provider_options))
            running = keeper.codex_processes_running()
            current_provider = keeper.detect_current_provider(codex_home)
            providers = read_session_provider_counts(codex_home)
            db = read_state_summary(codex_home)
            target_provider = current_provider if target_mode == "current" else target_mode
            needs_repair, reason = keeper.needs_ui_index_repair(codex_home, provider_mode=target_mode)

            lines = [
                f"Codex home: {codex_home}",
                f"Codex 进程: {'运行中 ' + ', '.join(running) if running else '未运行'}",
                f"当前配置 provider: {current_provider}",
                f"本次修复目标 provider: {target_provider} ({target_mode})",
                f"会话 JSONL 数量: {sum(providers.values())}",
                "JSONL provider 分布:",
            ]
            lines.extend([f"  - {name}: {count}" for name, count in providers.most_common()] or ["  - 无"])
            lines.extend(
                [
                    "",
                    f"state_5.sqlite: {'存在' if db['exists'] else '缺失'}",
                    f"SQLite quick_check: {db['quick_check']}",
                    f"SQLite 更新时间: {db['mtime']}",
                    f"线程总数: {db['thread_count']}",
                    "",
                    "SQLite provider 分布:",
                ]
            )
            for provider, source, archived, has_user_event, count in db["provider_groups"]:
                lines.append(f"  - {provider} / {source} / archived={archived} / user={has_user_event}: {count}")
            if not db["provider_groups"]:
                lines.append("  - 无")

            lines.extend(["", "按项目可显示数量（未归档且有用户消息）:"])
            for cwd, count in db["visible_projects"]:
                lines.append(f"  - {strip_extended_path(cwd)}: {count}")
            if not db["visible_projects"]:
                lines.append("  - 无")

            lines.extend(["", f"修复建议: {'需要修复' if needs_repair else '看起来正常'}"])
            lines.append(f"原因: {reason}")
            return "\n".join(lines)
        except Exception as exc:
            raise exc

    def refresh_log_loop(self) -> None:
        text = latest_log_tail(REPAIR_LOG)
        if text != self._last_log_text:
            self._last_log_text = text
            self.set_text(self.log_text, text)
        self.after(2500, self.refresh_log_loop)

    def start_watcher(self) -> None:
        if self.watcher_running:
            self.set_status("已经在等待 Codex 关闭，请直接关闭 Codex")
            return
        self.watcher_running = True
        target_mode = self.provider_mode()
        self.set_status("已开始等待：请关闭 Codex，关闭后会自动修复并重启")

        def worker() -> None:
            try:
                write_repair_log(f"GUI watcher started with provider mode: {target_mode}")
                while keeper.codex_processes_running():
                    time.sleep(1.0)
                time.sleep(2.0)
                result = keeper.repair_ui_index(self.config_data, apply=True, provider_mode=target_mode)
                write_repair_log(f"GUI watcher repaired {result.session_count} sessions; provider={result.current_provider}")
                keeper.launch_codex(self.config_data)
                self.after(0, lambda: self.set_status(f"修复完成并已重启 Codex：{result.session_count} 条会话"))
                self.after(0, self.refresh_diagnostics)
            except Exception as exc:
                message = str(exc)
                write_repair_log(traceback.format_exc())
                self.after(0, lambda message=message: messagebox.showerror(APP_TITLE, message))
                self.after(0, lambda: self.set_status("自动修复失败"))
            finally:
                self.watcher_running = False

        threading.Thread(target=worker, daemon=True).start()
        return

    def start_external_watcher(self) -> None:
        try:
            if not WATCHER_SCRIPT.exists():
                raise FileNotFoundError(WATCHER_SCRIPT)
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(WATCHER_SCRIPT),
                    "-ProviderMode",
                    self.provider_mode(),
                ],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
            )
            self.set_status("已启动等待修复：请关闭 Codex，工具会自动修复并重新打开")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self.set_status("启动等待修复失败")

    def apply_now(self) -> None:
        if self.repair_running:
            self.set_status("修复正在后台进行...")
            return
        self.repair_running = True
        self.set_status("正在后台修复...")
        target_mode = self.provider_mode()

        def worker() -> None:
            try:
                if keeper.codex_processes_running():
                    self.after(
                        0,
                        lambda: messagebox.showwarning(APP_TITLE, "Codex 正在运行。\n\n请先关闭 Codex，或点击“关闭后自动修复”。"),
                    )
                    self.after(0, lambda: self.set_status("Codex 正在运行，已暂停立即修复"))
                    return
                result = keeper.repair_ui_index(self.config_data, apply=True, provider_mode=target_mode)
                self.after(0, lambda: self.set_status(f"修复完成：{result.session_count} 条会话，备份在 {result.backup_dir}"))
                self.after(0, self.refresh_diagnostics)
                self.after(0, lambda: messagebox.showinfo(APP_TITLE, "修复完成。现在可以打开 Codex。"))
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_TITLE, message))
                self.after(0, lambda: self.set_status("修复失败"))
            finally:
                self.repair_running = False

        threading.Thread(target=worker, daemon=True).start()

    def export_vault(self) -> None:
        if self.export_running:
            self.set_status("对话备份正在后台进行...")
            return
        self.export_running = True
        self.set_status("正在备份对话...")

        def worker() -> None:
            try:
                result = keeper.sync_vault(self.config_data)
                self.after(0, lambda: self.set_status(f"已备份 {result.session_count} 条对话到 {result.out_dir}"))
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_TITLE, message))
                self.after(0, lambda: self.set_status("备份失败"))
            finally:
                self.export_running = False

        threading.Thread(target=worker, daemon=True).start()

    def open_vault(self) -> None:
        vault = keeper.latest_vault(Path(self.config_data["vault_root"]).expanduser())
        keeper.open_path(vault or Path(self.config_data["vault_root"]).expanduser())

    def create_shortcut(self) -> None:
        try:
            path = create_gui_shortcut()
            self.set_status(f"已创建桌面图标：{path}")
            messagebox.showinfo(APP_TITLE, f"已创建桌面图标：\n{path}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))


def main() -> int:
    app = RepairGui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
