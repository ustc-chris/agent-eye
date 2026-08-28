#!/usr/bin/env python3
"""Tkinter front end for the multi-machine Agent Eye NPU dashboard."""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

import npu_dashboard as dashboard

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except (ImportError, ModuleNotFoundError):  # Keep config helpers usable headlessly.
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]


APP_NAME = "agent_eye"
CONFIG_FILENAME = "npu_dashboard_tkinter.json"
TK_POLL_MILLISECONDS = 250
SYSTEM_THEME_POLL_SECONDS = 5.0
THEME_MODES = ("auto", "dark", "light")
THEME_LABELS = {"auto": "自动", "dark": "深色", "light": "浅色"}


@dataclass(frozen=True)
class ThemePalette:
    background: str
    panel: str
    text: str
    muted: str
    cyan: str
    green: str
    yellow: str
    red: str
    blue: str
    selection: str


DARK_PALETTE = ThemePalette(
    background="#10151c",
    panel="#18212b",
    text="#e8edf2",
    muted="#8b98a5",
    cyan="#38bdf8",
    green="#22c55e",
    yellow="#eab308",
    red="#ef4444",
    blue="#60a5fa",
    selection="#075985",
)
LIGHT_PALETTE = ThemePalette(
    background="#f3f6f9",
    panel="#ffffff",
    text="#17212b",
    muted="#64717d",
    cyan="#0369a1",
    green="#15803d",
    yellow="#a16207",
    red="#b91c1c",
    blue="#1d4ed8",
    selection="#bae6fd",
)


@dataclass(frozen=True)
class DashboardConfig:
    query_refresh_seconds: float
    display_columns: int
    machines: tuple[dashboard.Machine, ...]
    remote_eye_command: str
    ssh_timeout_seconds: float
    always_on_top: bool
    theme_mode: str

    @classmethod
    def defaults(cls) -> "DashboardConfig":
        return cls(
            query_refresh_seconds=float(dashboard.QUERY_REFRESH_SECONDS),
            display_columns=int(dashboard.DISPLAY_COLUMNS),
            machines=dashboard.load_machines(dashboard.MACHINES),
            remote_eye_command=dashboard.REMOTE_EYE_COMMAND,
            ssh_timeout_seconds=float(dashboard.SSH_TIMEOUT_SECONDS),
            always_on_top=False,
            theme_mode="auto",
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "DashboardConfig":
        try:
            query_refresh = float(values["query_refresh_seconds"])
            columns = int(values["display_columns"])
            timeout = float(values["ssh_timeout_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("刷新周期、列数和超时必须是有效数字") from exc
        if not math.isfinite(query_refresh) or query_refresh <= 0:
            raise ValueError("远端查询周期必须大于 0")
        if columns <= 0:
            raise ValueError("最大分列数必须大于 0")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("SSH 超时必须大于 0")

        command = values.get("remote_eye_command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("远端命令不能为空")
        always_on_top = values.get("always_on_top", False)
        if not isinstance(always_on_top, bool):
            raise ValueError("窗口置顶设置必须是布尔值")
        theme_mode = values.get("theme_mode", "auto")
        if theme_mode not in THEME_MODES:
            raise ValueError("主题设置必须是 auto、dark 或 light")
        raw_machines = values.get("machines")
        if not isinstance(raw_machines, (list, tuple)):
            raise ValueError("机器列表必须是列表")
        machines = dashboard.load_machines(raw_machines)
        return cls(
            query_refresh,
            columns,
            machines,
            command.strip(),
            timeout,
            always_on_top,
            str(theme_mode),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "query_refresh_seconds": self.query_refresh_seconds,
            "display_columns": self.display_columns,
            "machines": [
                {"name": item.name, "ip": item.ip, "alias": item.alias}
                for item in self.machines
            ],
            "remote_eye_command": self.remote_eye_command,
            "ssh_timeout_seconds": self.ssh_timeout_seconds,
            "always_on_top": self.always_on_top,
            "theme_mode": self.theme_mode,
        }


def system_prefers_dark() -> bool:
    """Best-effort detection using each desktop's native preference source."""
    system = platform.system()
    if system == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        except (ImportError, OSError, ValueError):
            return False
    if system == "Darwin":
        try:
            completed = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and "dark" in completed.stdout.lower()

    gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if "dark" in gtk_theme:
        return True
    try:
        completed = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and "prefer-dark" in completed.stdout.lower()


def resolved_theme(theme_mode: str) -> str:
    if theme_mode == "auto":
        return "dark" if system_prefers_dark() else "light"
    return theme_mode


def next_theme_mode(theme_mode: str) -> str:
    return THEME_MODES[(THEME_MODES.index(theme_mode) + 1) % len(THEME_MODES)]


def palette_for(theme_mode: str) -> ThemePalette:
    return DARK_PALETTE if theme_mode == "dark" else LIGHT_PALETTE


def system_cache_directory() -> Path:
    """Return the current platform's conventional per-user cache directory."""
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA")
        return Path(root) if root else Path.home() / "AppData" / "Local"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches"
    root = os.environ.get("XDG_CACHE_HOME")
    return Path(root) if root else Path.home() / ".cache"


def config_path() -> Path:
    return system_cache_directory() / APP_NAME / CONFIG_FILENAME


def load_config(path: Path | None = None) -> DashboardConfig:
    target = path or config_path()
    if not target.exists():
        return DashboardConfig.defaults()
    try:
        values = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取设置文件 {target}：{exc}") from exc
    if not isinstance(values, dict):
        raise ValueError(f"设置文件 {target} 的顶层必须是对象")
    return DashboardConfig.from_mapping(values)


def save_config(config: DashboardConfig, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, target)
    except OSError:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
        raise
    return target


def query_machine(
    machine: dashboard.Machine, config: DashboardConfig
) -> dashboard.MachineResult:
    """Query one machine using an immutable settings snapshot."""
    arguments = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(config.ssh_timeout_seconds))}",
        machine.ip,
        config.remote_eye_command,
    ]
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.ssh_timeout_seconds,
        )
    except FileNotFoundError:
        return dashboard.MachineResult(error="本机未安装 ssh")
    except subprocess.TimeoutExpired:
        return dashboard.MachineResult(
            error=f"查询超时（{config.ssh_timeout_seconds:g}s）"
        )
    except OSError as exc:
        return dashboard.MachineResult(error=f"SSH 启动失败：{exc}")
    if completed.returncode != 0:
        details = completed.stderr.strip().splitlines()
        message = details[-1] if details else f"远端退出码 {completed.returncode}"
        return dashboard.MachineResult(error=dashboard._safe_text(message))
    rows = dashboard.parse_eye_output(completed.stdout)
    if not rows:
        return dashboard.MachineResult(error="远端未返回有效的 NPU 状态")
    return dashboard.MachineResult(rows=rows)


if tk is not None:

    class MachineEditor(tk.Toplevel):
        def __init__(
            self,
            parent: tk.Misc,
            title: str,
            initial: dashboard.Machine | None = None,
        ) -> None:
            super().__init__(parent)
            self.title(title)
            self.resizable(False, False)
            self.transient(parent)
            self.result: dashboard.Machine | None = None
            self.variables = {
                "name": tk.StringVar(value=initial.name if initial else ""),
                "ip": tk.StringVar(value=initial.ip if initial else ""),
                "alias": tk.StringVar(value=initial.alias if initial else ""),
            }
            body = ttk.Frame(self, padding=16)
            body.grid(sticky="nsew")
            for row, (key, label) in enumerate(
                (("name", "名称"), ("ip", "SSH 地址"), ("alias", "别名（可选）"))
            ):
                ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=5)
                entry = ttk.Entry(body, textvariable=self.variables[key], width=38)
                entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
                if row == 0:
                    entry.focus_set()
            buttons = ttk.Frame(body)
            buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
            ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
            ttk.Button(buttons, text="确定", command=self._accept).pack(
                side="right", padx=(0, 8)
            )
            self.bind("<Return>", lambda _event: self._accept())
            self.bind("<Escape>", lambda _event: self.destroy())
            self.grab_set()
            self.wait_visibility()

        def _accept(self) -> None:
            try:
                machine = dashboard.load_machines(
                    [
                        {
                            "name": self.variables["name"].get(),
                            "ip": self.variables["ip"].get(),
                            "alias": self.variables["alias"].get(),
                        }
                    ]
                )[0]
            except ValueError as exc:
                messagebox.showerror("机器配置无效", str(exc), parent=self)
                return
            self.result = machine
            self.destroy()


    class SettingsDialog(tk.Toplevel):
        def __init__(
            self,
            parent: tk.Misc,
            config: DashboardConfig,
            on_saved: Callable[[DashboardConfig], None],
        ) -> None:
            super().__init__(parent)
            self.title("设置")
            self.geometry("760x560")
            self.minsize(680, 500)
            self.transient(parent)
            self.on_saved = on_saved
            self.machines = list(config.machines)
            self.always_on_top = config.always_on_top
            self.theme_mode = config.theme_mode
            self.variables = {
                "query_refresh_seconds": tk.StringVar(
                    value=f"{config.query_refresh_seconds:g}"
                ),
                "display_columns": tk.StringVar(value=str(config.display_columns)),
                "remote_eye_command": tk.StringVar(value=config.remote_eye_command),
                "ssh_timeout_seconds": tk.StringVar(
                    value=f"{config.ssh_timeout_seconds:g}"
                ),
            }
            body = ttk.Frame(self, padding=16)
            body.pack(fill="both", expand=True)
            general = ttk.LabelFrame(body, text="刷新与查询", padding=12)
            general.pack(fill="x")
            fields = (
                ("query_refresh_seconds", "远端查询周期（秒）"),
                ("display_columns", "期望最大分列数"),
                ("remote_eye_command", "远端命令"),
                ("ssh_timeout_seconds", "SSH 超时（秒）"),
            )
            for row, (key, label) in enumerate(fields):
                ttk.Label(general, text=label).grid(row=row, column=0, sticky="w", pady=4)
                ttk.Entry(general, textvariable=self.variables[key]).grid(
                    row=row, column=1, sticky="ew", padx=(14, 0), pady=4
                )
            general.columnconfigure(1, weight=1)

            machines_frame = ttk.LabelFrame(body, text="机器列表", padding=10)
            machines_frame.pack(fill="both", expand=True, pady=(14, 0))
            self.tree = ttk.Treeview(
                machines_frame,
                columns=("name", "ip", "alias"),
                show="headings",
                selectmode="browse",
                height=8,
            )
            for key, label, width in (
                ("name", "名称", 150),
                ("ip", "SSH 地址", 260),
                ("alias", "别名", 180),
            ):
                self.tree.heading(key, text=label)
                self.tree.column(key, width=width, minwidth=80)
            self.tree.pack(side="left", fill="both", expand=True)
            scrollbar = ttk.Scrollbar(
                machines_frame, orient="vertical", command=self.tree.yview
            )
            scrollbar.pack(side="left", fill="y")
            self.tree.configure(yscrollcommand=scrollbar.set)
            actions = ttk.Frame(machines_frame)
            actions.pack(side="left", fill="y", padx=(10, 0))
            ttk.Button(actions, text="添加", command=self._add_machine).pack(fill="x")
            ttk.Button(actions, text="编辑", command=self._edit_machine).pack(
                fill="x", pady=6
            )
            ttk.Button(actions, text="删除", command=self._delete_machine).pack(fill="x")
            self.tree.bind("<Double-1>", lambda _event: self._edit_machine())
            self._refresh_tree()

            footer = ttk.Frame(body)
            footer.pack(fill="x", pady=(14, 0))
            ttk.Label(footer, text=f"设置保存至：{config_path()}").pack(side="left")
            ttk.Button(footer, text="取消", command=self.destroy).pack(side="right")
            ttk.Button(footer, text="保存并应用", command=self._save).pack(
                side="right", padx=(0, 8)
            )
            self.bind("<Escape>", lambda _event: self.destroy())
            self.grab_set()

        def _selected_index(self) -> int | None:
            selected = self.tree.selection()
            return int(selected[0]) if selected else None

        def _refresh_tree(self, selected: int | None = None) -> None:
            self.tree.delete(*self.tree.get_children())
            for index, machine in enumerate(self.machines):
                self.tree.insert(
                    "", "end", iid=str(index), values=(machine.name, machine.ip, machine.alias)
                )
            if selected is not None and 0 <= selected < len(self.machines):
                self.tree.selection_set(str(selected))

        def _add_machine(self) -> None:
            dialog = MachineEditor(self, "添加机器")
            self.wait_window(dialog)
            if dialog.result is not None:
                self.machines.append(dialog.result)
                self._refresh_tree(len(self.machines) - 1)

        def _edit_machine(self) -> None:
            index = self._selected_index()
            if index is None:
                messagebox.showinfo("编辑机器", "请先选择一台机器", parent=self)
                return
            dialog = MachineEditor(self, "编辑机器", self.machines[index])
            self.wait_window(dialog)
            if dialog.result is not None:
                self.machines[index] = dialog.result
                self._refresh_tree(index)

        def _delete_machine(self) -> None:
            index = self._selected_index()
            if index is None:
                messagebox.showinfo("删除机器", "请先选择一台机器", parent=self)
                return
            del self.machines[index]
            self._refresh_tree(min(index, len(self.machines) - 1))

        def _save(self) -> None:
            values: dict[str, object] = {
                key: variable.get() for key, variable in self.variables.items()
            }
            values["always_on_top"] = self.always_on_top
            values["theme_mode"] = self.theme_mode
            values["machines"] = [
                {"name": item.name, "ip": item.ip, "alias": item.alias}
                for item in self.machines
            ]
            try:
                config = DashboardConfig.from_mapping(values)
                save_config(config)
            except (ValueError, OSError) as exc:
                messagebox.showerror("保存设置失败", str(exc), parent=self)
                return
            self.on_saved(config)
            self.destroy()


    class DashboardApp:
        def __init__(self, root: tk.Tk, config: DashboardConfig) -> None:
            self.root = root
            self.config = config
            self.resolved_theme = resolved_theme(config.theme_mode)
            self.palette = palette_for(self.resolved_theme)
            self.next_theme_check = time.monotonic() + SYSTEM_THEME_POLL_SECONDS
            self.results: dict[dashboard.Machine, dashboard.MachineResult] = {}
            self.active: dict[Future[dashboard.MachineResult], dashboard.Machine] = {}
            self.last_refresh: datetime | None = None
            self.next_refresh = time.monotonic()
            self.after_id: str | None = None
            self.executor = self._new_executor()

            root.title("Agent Eye · NPU Dashboard")
            root.geometry("1060x720")
            root.minsize(620, 420)
            root.configure(background=self.palette.background)
            root.attributes("-topmost", config.always_on_top)
            root.protocol("WM_DELETE_WINDOW", self.close)
            self._configure_style()
            self._build_interface()
            self._render_all()
            self._tick()

        def _new_executor(self) -> ThreadPoolExecutor:
            return ThreadPoolExecutor(max_workers=max(1, len(self.config.machines)))

        def _configure_style(self) -> None:
            style = ttk.Style(self.root)
            if "clam" in style.theme_names():
                style.theme_use("clam")
            palette = self.palette
            style.configure("TFrame", background=palette.background)
            style.configure(
                "TLabel", background=palette.background, foreground=palette.text
            )
            style.configure(
                "TButton",
                background=palette.panel,
                foreground=palette.text,
                bordercolor=palette.muted,
            )
            style.map(
                "TButton",
                background=[("active", palette.selection)],
                foreground=[("active", palette.text)],
            )
            style.configure(
                "TCheckbutton", background=palette.background, foreground=palette.text
            )
            style.map(
                "TCheckbutton",
                background=[("active", palette.background)],
                foreground=[("active", palette.text)],
            )
            style.configure(
                "TLabelframe",
                background=palette.background,
                foreground=palette.text,
                bordercolor=palette.muted,
            )
            style.configure(
                "TLabelframe.Label",
                background=palette.background,
                foreground=palette.text,
            )
            style.configure(
                "TEntry",
                fieldbackground=palette.panel,
                foreground=palette.text,
                insertcolor=palette.text,
            )
            style.configure(
                "Treeview",
                background=palette.panel,
                fieldbackground=palette.panel,
                foreground=palette.text,
            )
            style.map(
                "Treeview",
                background=[("selected", palette.selection)],
                foreground=[("selected", palette.text)],
            )
            style.configure(
                "Treeview.Heading",
                background=palette.background,
                foreground=palette.text,
            )
            style.configure("Dashboard.TFrame", background=palette.background)
            style.configure("Panel.TFrame", background=palette.panel)
            style.configure(
                "Title.TLabel",
                background=palette.background,
                foreground=palette.cyan,
                font=("TkDefaultFont", 18, "bold"),
            )
            style.configure(
                "Body.TLabel", background=palette.background, foreground=palette.text
            )
            style.configure(
                "Muted.TLabel", background=palette.background, foreground=palette.muted
            )
            style.configure(
                "Panel.TLabel", background=palette.panel, foreground=palette.text
            )
            style.configure("Settings.TButton", padding=(12, 7))

        def _build_interface(self) -> None:
            header = ttk.Frame(self.root, style="Dashboard.TFrame", padding=(18, 14, 18, 8))
            header.pack(fill="x")
            ttk.Label(header, text="Agent Eye · NPU Dashboard", style="Title.TLabel").pack(side="left")
            ttk.Button(
                header,
                text="⚙ 设置",
                style="Settings.TButton",
                command=self.open_settings,
            ).pack(side="right")
            self.theme_text = tk.StringVar()
            self._update_theme_button_text()
            ttk.Button(
                header,
                textvariable=self.theme_text,
                command=self._cycle_theme,
            ).pack(side="right", padx=(0, 10))
            self.always_on_top = tk.BooleanVar(value=self.config.always_on_top)
            ttk.Checkbutton(
                header,
                text="窗口置顶",
                variable=self.always_on_top,
                command=self._toggle_always_on_top,
            ).pack(side="right", padx=(0, 12))

            self.machine_list = ttk.Frame(self.root, style="Dashboard.TFrame", padding=(18, 4, 18, 8))
            self.machine_list.pack(fill="x")

            container = ttk.Frame(self.root, style="Dashboard.TFrame")
            container.pack(fill="both", expand=True, padx=18)
            self.canvas = tk.Canvas(
                container,
                background=self.palette.background,
                highlightthickness=0,
            )
            scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
            self.cards = tk.Frame(
                self.canvas, background=self.palette.background
            )
            self.cards_window = self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
            self.canvas.configure(yscrollcommand=scrollbar.set)
            self.canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            self.cards.bind("<Configure>", self._on_cards_configure)
            self.canvas.bind("<Configure>", self._on_canvas_configure)

            self.footer_text = tk.StringVar()
            ttk.Label(
                self.root,
                textvariable=self.footer_text,
                style="Muted.TLabel",
                padding=(18, 9, 18, 12),
            ).pack(fill="x")

        def _on_cards_configure(self, _event: tk.Event) -> None:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _on_canvas_configure(self, event: tk.Event) -> None:
            self.canvas.itemconfigure(self.cards_window, width=event.width)
            self._render_cards()

        def _status_color(self, result: dashboard.MachineResult | None) -> str:
            if result is None:
                return self.palette.cyan
            if result.error:
                return self.palette.red
            free = sum(row.status == "FREE" for row in result.rows)
            if free == 0:
                return self.palette.red
            return self.palette.yellow if free <= 3 else self.palette.green

        def _render_machine_list(self) -> None:
            for child in self.machine_list.winfo_children():
                child.destroy()
            ttk.Label(self.machine_list, text="机器列表：", style="Body.TLabel").pack(anchor="w")
            if not self.config.machines:
                ttk.Label(
                    self.machine_list,
                    text="  暂无机器，请点击右上角“设置”添加",
                    style="Muted.TLabel",
                ).pack(anchor="w", pady=(3, 0))
            for index, machine in enumerate(self.config.machines, start=1):
                ttk.Label(
                    self.machine_list,
                    text=f"  {index}. {machine.label}  [{machine.ip}]",
                    style="Body.TLabel",
                ).pack(anchor="w", pady=(2, 0))

        def _render_cards(self) -> None:
            for child in self.cards.winfo_children():
                child.destroy()
            if not self.config.machines:
                return
            available_width = max(1, self.canvas.winfo_width())
            min_card_width = 300
            max_by_width = max(1, available_width // min_card_width)
            columns = min(self.config.display_columns, max_by_width, len(self.config.machines))
            for column in range(columns):
                self.cards.grid_columnconfigure(column, weight=1, uniform="cards")
            for index, machine in enumerate(self.config.machines):
                self._build_card(machine).grid(
                    row=index // columns,
                    column=index % columns,
                    sticky="nsew",
                    padx=6,
                    pady=6,
                )

        def _build_card(self, machine: dashboard.Machine) -> tk.Frame:
            result = self.results.get(machine)
            color = self._status_color(result)
            card = tk.Frame(
                self.cards,
                background=self.palette.panel,
                highlightbackground=color,
                highlightcolor=color,
                highlightthickness=2,
                padx=10,
                pady=9,
            )
            header = tk.Label(
                card,
                text=f"{machine.label}     free: {dashboard._availability(result)}",
                background=self.palette.panel,
                foreground=color,
                font=("TkDefaultFont", 11, "bold"),
            )
            header.pack(fill="x", pady=(0, 8))
            if result is None:
                text = "正在查询..." if self.active else "等待查询..."
                tk.Label(
                    card,
                    text=text,
                    background=self.palette.panel,
                    foreground=self.palette.muted,
                    anchor="w",
                ).pack(fill="x")
                return card
            if result.error:
                tk.Label(
                    card,
                    text=f"ERROR: {result.error}",
                    background=self.palette.panel,
                    foreground=self.palette.red,
                    anchor="w",
                    justify="left",
                    wraplength=480,
                ).pack(fill="x")
                return card

            table = tk.Frame(card, background=color)
            table.pack(fill="x")
            for column, weight in ((0, 1), (1, 3)):
                table.grid_columnconfigure(column, weight=weight)
            for column, title in enumerate(("NPU", "STATUS")):
                tk.Label(
                    table,
                    text=title,
                    background=self.palette.panel,
                    foreground=color,
                    anchor="w",
                    padx=7,
                    pady=4,
                ).grid(row=0, column=column, sticky="nsew", padx=1, pady=1)
            for row_index, row in enumerate(result.rows, start=1):
                content_color = (
                    self.palette.blue if row.status == "FREE" else self.palette.red
                )
                for column, value in enumerate((f"NPU {row.npu_id}", row.display_status)):
                    tk.Label(
                        table,
                        text=value,
                        background=self.palette.panel,
                        foreground=content_color,
                        anchor="w",
                        padx=7,
                        pady=4,
                    ).grid(row=row_index, column=column, sticky="nsew", padx=1, pady=1)
            return card

        def _render_footer(self) -> None:
            refresh = "正在查询" if self.active else f"{max(0.0, self.next_refresh - time.monotonic()):.1f}s"
            self.footer_text.set(
                f"Last refresh: {dashboard._format_time(self.last_refresh)}\n"
                f"Next refresh: {refresh}/{self.config.query_refresh_seconds:g}s"
            )

        def _render_all(self) -> None:
            self._render_machine_list()
            self._render_cards()
            self._render_footer()

        def _update_theme_button_text(self) -> None:
            self.theme_text.set(f"主题：{THEME_LABELS[self.config.theme_mode]}")

        def _apply_theme(self, resolved: str) -> None:
            self.resolved_theme = resolved
            self.palette = palette_for(resolved)
            self.root.configure(background=self.palette.background)
            self._configure_style()
            self.canvas.configure(background=self.palette.background)
            self.cards.configure(background=self.palette.background)
            self._update_theme_button_text()
            self._render_all()

        def _cycle_theme(self) -> None:
            updated = replace(
                self.config,
                theme_mode=next_theme_mode(self.config.theme_mode),
            )
            try:
                save_config(updated)
            except OSError as exc:
                messagebox.showerror("保存设置失败", str(exc), parent=self.root)
                return
            self.config = updated
            self.next_theme_check = time.monotonic() + SYSTEM_THEME_POLL_SECONDS
            self._apply_theme(resolved_theme(updated.theme_mode))

        def _follow_system_theme(self) -> None:
            now = time.monotonic()
            if self.config.theme_mode != "auto" or now < self.next_theme_check:
                return
            self.next_theme_check = now + SYSTEM_THEME_POLL_SECONDS
            current = resolved_theme("auto")
            if current != self.resolved_theme:
                self._apply_theme(current)

        def _start_query(self) -> None:
            if self.active or not self.config.machines:
                return
            snapshot = self.config
            self.active = {
                self.executor.submit(query_machine, machine, snapshot): machine
                for machine in snapshot.machines
            }
            self._render_cards()

        def _collect_results(self) -> None:
            if not self.active:
                return
            changed = False
            for future, machine in tuple(self.active.items()):
                if not future.done():
                    continue
                try:
                    self.results[machine] = future.result()
                except Exception as exc:
                    self.results[machine] = dashboard.MachineResult(
                        error=f"内部查询错误：{dashboard._safe_text(str(exc))}"
                    )
                del self.active[future]
                changed = True
            if not self.active:
                self.last_refresh = datetime.now()
                self.next_refresh = time.monotonic() + self.config.query_refresh_seconds
                changed = True
            if changed:
                self._render_cards()

        def _tick(self) -> None:
            self._follow_system_theme()
            self._collect_results()
            if not self.active and time.monotonic() >= self.next_refresh:
                self._start_query()
            self._render_footer()
            self.after_id = self.root.after(TK_POLL_MILLISECONDS, self._tick)

        def open_settings(self) -> None:
            SettingsDialog(self.root, self.config, self.apply_config)

        def _toggle_always_on_top(self) -> None:
            enabled = self.always_on_top.get()
            self.root.attributes("-topmost", enabled)
            updated = replace(self.config, always_on_top=enabled)
            try:
                save_config(updated)
            except OSError as exc:
                self.always_on_top.set(self.config.always_on_top)
                self.root.attributes("-topmost", self.config.always_on_top)
                messagebox.showerror(
                    "保存设置失败", str(exc), parent=self.root
                )
                return
            self.config = updated

        def apply_config(self, config: DashboardConfig) -> None:
            old_executor = self.executor
            self.config = config
            self.always_on_top.set(config.always_on_top)
            self.root.attributes("-topmost", config.always_on_top)
            self.next_theme_check = time.monotonic() + SYSTEM_THEME_POLL_SECONDS
            self._apply_theme(resolved_theme(config.theme_mode))
            self.executor = self._new_executor()
            self.active.clear()
            self.results.clear()
            self.last_refresh = None
            self.next_refresh = time.monotonic()
            old_executor.shutdown(wait=False, cancel_futures=True)
            self._render_all()

        def close(self) -> None:
            if self.after_id is not None:
                self.root.after_cancel(self.after_id)
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.root.destroy()


def main() -> int:
    if tk is None:
        print(
            "npu_dashboard_tkinter: 当前 Python 未安装 Tk 支持（tkinter/_tkinter）",
            file=sys.stderr,
        )
        return 2
    warning: str | None = None
    try:
        config = load_config()
    except ValueError as exc:
        config = DashboardConfig.defaults()
        warning = str(exc)
    root = tk.Tk()
    DashboardApp(root, config)
    if warning:
        root.after(
            0,
            lambda: messagebox.showwarning(
                "设置文件无效", warning + "\n\n已使用脚本默认设置。", parent=root
            ),
        )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
