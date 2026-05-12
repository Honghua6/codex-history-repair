#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Visual repair console for local Codex history visibility issues."""

from __future__ import annotations

import collections
import datetime as dt
import os
import sqlite3
import subprocess
import sys
import threading
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
        self.watcher_process = None
        self.export_running = False
        self._last_log_text = ""
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(860, 620)
        self.configure(bg="#f6f7f9")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self._setup_style()
        self._build_ui()
        self.set_health_banner("idle", "等待诊断", "点击“刷新诊断”检查当前状态。")
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

        banner = tk.Frame(self, bg="#f3f4f6", highlightthickness=1, highlightbackground="#d1d5db")
        banner.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 12))
        banner.columnconfigure(1, weight=1)
        self.health_icon_var = tk.StringVar(value="-")
        self.health_title_var = tk.StringVar(value="等待诊断")
        self.health_detail_var = tk.StringVar(value="")
        self.health_icon_label = tk.Label(
            banner,
            textvariable=self.health_icon_var,
            font=("Segoe UI", 18, "bold"),
            bg="#f3f4f6",
            fg="#374151",
            width=4,
            anchor="center",
        )
        self.health_icon_label.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(14, 10), pady=10)
        self.health_title_label = tk.Label(
            banner,
            textvariable=self.health_title_var,
            font=("Segoe UI", 12, "bold"),
            bg="#f3f4f6",
            fg="#111827",
            anchor="w",
        )
        self.health_title_label.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(10, 2))
        self.health_detail_label = tk.Label(
            banner,
            textvariable=self.health_detail_var,
            font=("Segoe UI", 10),
            bg="#f3f4f6",
            fg="#4b5563",
            anchor="w",
            justify="left",
        )
        self.health_detail_label.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=(0, 10))

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.grid(row=3, column=0, sticky="nsew", padx=18, pady=(0, 12))

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
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(footer, textvariable=self.status_var, style="Hint.TLabel").grid(row=0, column=0, sticky="w")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_health_banner(self, state: str, title: str, detail: str = "") -> None:
        palette = {
            "idle": {"bg": "#f3f4f6", "border": "#d1d5db", "title": "#111827", "detail": "#4b5563", "icon": "-"},
            "busy": {"bg": "#eff6ff", "border": "#bfdbfe", "title": "#1d4ed8", "detail": "#2563eb", "icon": "..."},
            "healthy": {"bg": "#ecfdf3", "border": "#a7f3d0", "title": "#166534", "detail": "#15803d", "icon": "OK"},
            "repair": {"bg": "#fef2f2", "border": "#fecaca", "title": "#991b1b", "detail": "#b91c1c", "icon": "!"},
            "warning": {"bg": "#fff7ed", "border": "#fed7aa", "title": "#9a3412", "detail": "#c2410c", "icon": "!"},
            "error": {"bg": "#fef2f2", "border": "#fca5a5", "title": "#991b1b", "detail": "#b91c1c", "icon": "X"},
        }
        colors = palette.get(state, palette["idle"])
        self.health_icon_var.set(colors["icon"])
        self.health_title_var.set(title)
        self.health_detail_var.set(detail)
        self.health_icon_label.configure(bg=colors["bg"], fg=colors["title"])
        self.health_title_label.configure(bg=colors["bg"], fg=colors["title"])
        self.health_detail_label.configure(bg=colors["bg"], fg=colors["detail"])
        self.health_icon_label.master.configure(bg=colors["bg"], highlightbackground=colors["border"])

    def set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def set_diagnosis_text(self, text: str, needs_repair: bool) -> None:
        self.diagnosis.configure(state="normal")
        self.diagnosis.delete("1.0", tk.END)
        self.diagnosis.insert("1.0", text)
        self.diagnosis.tag_configure("status_ok", foreground="#166534", font=("Consolas", 10, "bold"))
        self.diagnosis.tag_configure("status_bad", foreground="#b91c1c", font=("Consolas", 10, "bold"))
        self.diagnosis.tag_configure("reason_line", foreground="#9a3412")
        for index, line in enumerate(text.splitlines(), 1):
            if line.startswith("修复建议:"):
                tag = "status_bad" if needs_repair else "status_ok"
                self.diagnosis.tag_add(tag, f"{index}.0", f"{index}.end")
            elif line.startswith("原因:"):
                self.diagnosis.tag_add("reason_line", f"{index}.0", f"{index}.end")
        self.diagnosis.configure(state="disabled")

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
        self.set_health_banner("busy", "正在诊断", "正在检查本地 UI 索引、provider 和 SQLite 状态。")
        target_mode = self.provider_mode()

        def worker() -> None:
            try:
                report = self.build_diagnostics_report(target_mode)
                self.after(0, lambda report=report: self.set_diagnosis_text(str(report["text"]), bool(report["needs_repair"])))
                self.after(
                    0,
                    lambda report=report: self.set_health_banner(
                        "repair" if bool(report["needs_repair"]) else "healthy",
                        "需要修复" if bool(report["needs_repair"]) else "看起来正常",
                        str(report["reason"]),
                    ),
                )
                self.after(0, lambda: self.set_status("诊断已刷新"))
            except Exception as exc:
                trace = traceback.format_exc()
                message = str(exc)
                self.after(0, lambda: self.set_text(self.diagnosis, trace))
                self.after(0, lambda message=message: self.set_health_banner("error", "诊断失败", message))
                self.after(0, lambda message=message: self.set_status(f"诊断失败: {message}"))
            finally:
                self.diagnosis_running = False

        threading.Thread(target=worker, daemon=True).start()

    def build_diagnostics_report(self, target_mode: str) -> dict[str, object]:
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
            return {"text": "\n".join(lines), "needs_repair": needs_repair, "reason": reason}
        except Exception as exc:
            raise exc

    def refresh_log_loop(self) -> None:
        text = latest_log_tail(REPAIR_LOG)
        if text != self._last_log_text:
            self._last_log_text = text
            self.set_text(self.log_text, text)
        self.after(2500, self.refresh_log_loop)

    def poll_watcher_process(self) -> None:
        proc = self.watcher_process
        if proc is None:
            self.watcher_running = False
            return
        if proc.poll() is None:
            self.watcher_running = True
            self.after(1500, self.poll_watcher_process)
            return
        self.watcher_running = False
        self.watcher_process = None
        if proc.returncode not in (0, None):
            self.set_health_banner("warning", "等待修复已退出", "后台等待修复进程提前结束，请查看右侧日志。")
            self.set_status("等待修复进程已退出，请查看日志")
        else:
            self.set_health_banner("idle", "等待修复已结束", "可以重新诊断，或按需再次启动等待修复。")
            self.set_status("等待修复进程已结束，可按需重新启动")

    def start_watcher(self) -> None:
        if self.repair_running:
            self.set_health_banner("warning", "正在修复", "当前已有修复任务在后台运行，请稍候。")
            self.set_status("正在后台修复，请稍候")
            return
        if not keeper.codex_processes_running():
            messagebox.showinfo(APP_TITLE, "Codex 当前没有在运行。\n\n请直接使用“立即修复”，或先打开 Codex 再使用“关闭后自动修复”。")
            self.set_health_banner("warning", "Codex 未运行", "当前更适合直接使用“立即修复”。")
            self.set_status("Codex 当前未运行，请改用立即修复")
            return
        existing = self.watcher_process
        if existing is not None and existing.poll() is None:
            self.set_health_banner("busy", "等待关闭中", "后台已在等待 Codex 关闭，请直接关闭 Codex。")
            self.watcher_running = True
            self.set_status("已启动外部等待修复，请直接关闭 Codex")
            return
        self.watcher_running = True
        try:
            if not WATCHER_SCRIPT.exists():
                raise FileNotFoundError(WATCHER_SCRIPT)
            self.watcher_process = subprocess.Popen(
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
            self.set_health_banner("busy", "等待关闭后自动修复", "请关闭 Codex，工具会在关闭后自动修复并重新打开。")
            self.set_status("已启动等待修复：请关闭 Codex，工具会自动修复并重新打开")
            self.after(1500, self.poll_watcher_process)
        except Exception as exc:
            self.watcher_running = False
            self.watcher_process = None
            messagebox.showerror(APP_TITLE, str(exc))
            self.set_health_banner("error", "启动等待修复失败", str(exc))
            self.set_status("启动等待修复失败")

    def apply_now(self) -> None:
        if self.repair_running:
            self.set_health_banner("warning", "正在修复", "已有修复任务正在后台运行。")
            self.set_status("修复正在后台进行...")
            return
        existing = self.watcher_process
        if existing is not None and existing.poll() is None:
            self.set_health_banner("warning", "已有等待修复任务", "请先关闭 Codex，让现有后台任务完成。")
            self.set_status("已启动关闭后自动修复，请先关闭 Codex")
            messagebox.showinfo(APP_TITLE, "已存在一个“关闭后自动修复”任务。\n\n请先关闭 Codex，让后台任务完成；如果要立即修复，请先等待该任务结束。")
            return
        self.repair_running = True
        self.set_health_banner("busy", "正在修复", "正在重建 UI 索引并准备写回本地状态。")
        self.set_status("正在后台修复...")
        target_mode = self.provider_mode()

        def worker() -> None:
            try:
                if keeper.codex_processes_running():
                    self.after(
                        0,
                        lambda: messagebox.showwarning(APP_TITLE, "Codex 正在运行。\n\n请先关闭 Codex，或点击“关闭后自动修复”。"),
                    )
                    self.after(0, lambda: self.set_health_banner("warning", "Codex 正在运行", "请先关闭 Codex，或改用“关闭后自动修复”。"))
                    self.after(0, lambda: self.set_status("Codex 正在运行，已暂停立即修复"))
                    return
                result = keeper.repair_ui_index(self.config_data, apply=True, provider_mode=target_mode)
                self.after(0, lambda: self.set_health_banner("healthy", "修复完成", f"已修复 {result.session_count} 条会话，建议再看一眼最新诊断。"))
                self.after(0, lambda: self.set_status(f"修复完成：{result.session_count} 条会话，备份在 {result.backup_dir}"))
                self.after(0, self.refresh_diagnostics)
                self.after(0, lambda: messagebox.showinfo(APP_TITLE, "修复完成。现在可以打开 Codex。"))
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_TITLE, message))
                self.after(0, lambda message=message: self.set_health_banner("error", "修复失败", message))
                self.after(0, lambda: self.set_status("修复失败"))
            finally:
                self.repair_running = False

        threading.Thread(target=worker, daemon=True).start()

    def export_vault(self) -> None:
        if self.export_running:
            self.set_status("对话备份正在后台进行...")
            return
        self.export_running = True
        self.set_health_banner("busy", "正在备份", "正在导出本地对话历史到备份目录。")
        self.set_status("正在备份对话...")

        def worker() -> None:
            try:
                result = keeper.sync_vault(self.config_data)
                self.after(0, lambda: self.set_health_banner("healthy", "备份完成", f"已导出 {result.session_count} 条对话。"))
                self.after(0, lambda: self.set_status(f"已备份 {result.session_count} 条对话到 {result.out_dir}"))
            except Exception as exc:
                message = str(exc)
                self.after(0, lambda message=message: messagebox.showerror(APP_TITLE, message))
                self.after(0, lambda message=message: self.set_health_banner("error", "备份失败", message))
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
