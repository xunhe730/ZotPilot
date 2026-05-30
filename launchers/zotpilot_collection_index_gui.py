from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.exists():
    sys.path.insert(0, str(SRC_ROOT))

try:
    from zotpilot.config import Config
except Exception:  # pragma: no cover - import fallback for source checkouts without deps
    Config = None  # type: ignore[assignment]

DEFAULT_KEYS_ENV = "ZOTPILOT_INDEX_KEYS"
DEFAULT_INDEX_ROOT_ENV = "ZOTPILOT_INDEX_ROOT"
DEFAULT_ZOTPILOT_EXE_ENV = "ZOTPILOT_EXE"
MUPDF_STRUCTURE_TREE_WARNING = "MuPDF error: format error: No common ancestor in structure tree"


def default_index_root() -> Path:
    env_root = os.environ.get(DEFAULT_INDEX_ROOT_ENV)
    if env_root:
        return Path(env_root)
    if Config is not None:
        try:
            return Config.load().chroma_db_path.parent
        except Exception:
            pass
    return Path.home() / ".local" / "share" / "zotpilot"


def default_keys_path() -> Path | None:
    env_keys = os.environ.get(DEFAULT_KEYS_ENV)
    return Path(env_keys) if env_keys else None


def resolve_zotpilot_exe(override: Path | None = None) -> str:
    if override:
        return str(override)
    env_exe = os.environ.get(DEFAULT_ZOTPILOT_EXE_ENV)
    if env_exe:
        return env_exe
    nearby = Path(sys.executable).with_name("zotpilot.exe")
    if nearby.exists():
        return str(nearby)
    discovered = shutil.which("zotpilot")
    return discovered or "zotpilot"


@dataclass(frozen=True)
class PaperTask:
    index: int
    key: str
    title: str
    pdf_count: int
    collection_path: str = ""
    publication: str = ""
    date: str = ""
    item_type: str = ""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def bilingual(cn: str, en: str) -> str:
    return f"{cn}\n  EN: {en}"


def classify_runtime_warning(line: str, key: str) -> tuple[str, str] | None:
    text = line.strip()
    if text == MUPDF_STRUCTURE_TREE_WARNING:
        return (
            "mupdf_structure_tree",
            bilingual(
                f"PDF 结构树警告：{key} 的 PDF 标签结构不规范，"
                "ZotPilot 已继续抽取正文/图表；若最终状态为“索引成功”，不影响后续语义检索。",
                f"PDF structure warning: {key} has a malformed tagged-PDF structure tree; "
                "ZotPilot continued text/table extraction. If the final status is indexed, "
                "later semantic search is not affected.",
            ),
        )
    return None


def load_tasks(path: Path, *, include_no_pdf: bool) -> list[PaperTask]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks: list[PaperTask] = []
    for item in data:
        pdf_count = int(item.get("pdf_count") or 0)
        if pdf_count <= 0 and not include_no_pdf:
            continue
        tasks.append(
            PaperTask(
                index=int(item.get("index") or len(tasks) + 1),
                key=str(item.get("key", "")).strip(),
                title=str(item.get("title", "")).strip(),
                pdf_count=pdf_count,
                collection_path=str(item.get("collection_path", "")).strip(),
                publication=str(item.get("publication", "")).strip(),
                date=str(item.get("date", "")).strip(),
                item_type=str(item.get("type", "")).strip(),
            )
        )
    return [task for task in tasks if task.key]


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def parse_record_time(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def parse_index_result(text: str, returncode: int) -> tuple[str, str, dict[str, int]]:
    counts: dict[str, int] = {}
    for label, key in [
        ("Indexed", "indexed"),
        ("Already indexed", "already_indexed"),
        ("Skipped (empty)", "skipped"),
        ("Failed", "failed"),
        ("Empty", "empty"),
    ]:
        match = re.search(rf"{re.escape(label)}:\s*(\d+)", text)
        if match:
            counts[key] = int(match.group(1))

    if returncode != 0 and counts.get("indexed", 0) == 0 and counts.get("already_indexed", 0) == 0:
        return "error", f"zotpilot exited with code {returncode}", counts
    if counts.get("indexed", 0) > 0:
        return "indexed", "indexed into ChromaDB", counts
    if counts.get("already_indexed", 0) > 0:
        return "already_indexed", "already indexed, skipped by incremental indexer", counts
    if counts.get("failed", 0) > 0:
        return "failed", "indexer reported failed item", counts
    if counts.get("empty", 0) > 0 or counts.get("skipped", 0) > 0:
        return "skipped", "empty or skipped by indexer", counts
    return "unknown", "no explicit index count found", counts


class ZotPilotIndexWindow:
    def __init__(
        self,
        root: tk.Tk,
        tasks: list[PaperTask],
        *,
        log_path: Path,
        index_root: Path,
        zotpilot_exe: str,
        max_pages: int,
        enable_vision: bool,
        auto_start: bool,
    ) -> None:
        self.root = root
        self.tasks = tasks
        self.log_path = log_path
        self.index_root = index_root
        self.zotpilot_exe = zotpilot_exe
        self.max_pages = max_pages
        self.enable_vision = enable_vision
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_requested = threading.Event()
        self.worker: threading.Thread | None = None
        self.started = False
        self.paused = False
        self.completed_keys: set[str] = set()
        self.runtime_warning_seen: set[tuple[str, str]] = set()

        self.done = 0
        self.indexed = 0
        self.already = 0
        self.failed = 0
        self.skipped = 0
        self.started_at: float | None = None
        self.current_started_at: float | None = None
        self.last_heartbeat_at: float = 0.0
        self.current_title = "等待开始"

        root.title("zotero_mcp_zotpilot 语义全文索引任务")
        root.geometry("1720x900")
        root.minsize(1180, 660)

        self.title_label = ttk.Label(
            root,
            text="zotero_mcp_zotpilot 语义全文索引任务",
            font=("Segoe UI", 15, "bold"),
        )
        self.title_label.pack(anchor="w", padx=16, pady=(14, 4))

        mode = "Vision ON" if enable_vision else "Vision OFF"
        self.info_var = tk.StringVar(value=f"索引目录：{index_root} | max_pages={max_pages} | {mode}")
        ttk.Label(root, textvariable=self.info_var, foreground="#555").pack(anchor="w", padx=16)

        self.log_var = tk.StringVar(value=f"进度日志：{log_path}")
        ttk.Label(root, textvariable=self.log_var, foreground="#555").pack(anchor="w", padx=16, pady=(2, 8))

        controls = ttk.Frame(root)
        controls.pack(fill="x", padx=16, pady=(0, 10))

        self.start_button = ttk.Button(controls, text="开始索引", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="当前论文后停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.pause_button = ttk.Button(controls, text="暂停索引", command=self.pause, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.resume_button = ttk.Button(controls, text="继续索引", command=self.resume, state="disabled")
        self.resume_button.pack(side="left", padx=(8, 0))

        self.stats_var = tk.StringVar(value="准备就绪")
        ttk.Label(controls, textvariable=self.stats_var, font=("Segoe UI", 10)).pack(side="left", padx=16)

        detail_frame = ttk.LabelFrame(root, text="任务统计")
        detail_frame.pack(fill="x", padx=16, pady=(0, 10))

        self.total_var = tk.StringVar()
        self.done_detail_var = tk.StringVar()
        self.remaining_var = tk.StringVar()
        self.percent_var = tk.StringVar()
        self.elapsed_var = tk.StringVar()
        self.avg_var = tk.StringVar()
        self.eta_var = tk.StringVar()
        self.current_var = tk.StringVar()

        stat_items = [
            ("总文件数", self.total_var),
            ("已完成", self.done_detail_var),
            ("剩余文件数", self.remaining_var),
            ("完成百分比", self.percent_var),
            ("已用时间", self.elapsed_var),
            ("平均每篇", self.avg_var),
            ("预计剩余时间", self.eta_var),
        ]
        for idx, (label, variable) in enumerate(stat_items):
            ttk.Label(detail_frame, text=f"{label}：", foreground="#555").grid(
                row=idx // 4, column=(idx % 4) * 2, sticky="e", padx=(10, 2), pady=4
            )
            ttk.Label(detail_frame, textvariable=variable, font=("Segoe UI", 10, "bold")).grid(
                row=idx // 4, column=(idx % 4) * 2 + 1, sticky="w", padx=(0, 18), pady=4
            )

        ttk.Label(detail_frame, text="当前论文：", foreground="#555").grid(
            row=2, column=0, sticky="ne", padx=(10, 2), pady=4
        )
        ttk.Label(detail_frame, textvariable=self.current_var, wraplength=760).grid(
            row=2, column=1, columnspan=7, sticky="w", padx=(0, 10), pady=4
        )

        self.progress = ttk.Progressbar(root, maximum=max(len(tasks), 1), mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(0, 12))

        self.pane = tk.PanedWindow(root, orient=tk.VERTICAL, sashwidth=8, sashrelief="raised")
        self.pane.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        table_frame = ttk.Frame(self.pane)
        columns = ("index", "status", "collection", "key", "type", "date", "publication", "pdf", "seconds", "title")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=16)
        for col, text, width, anchor in [
            ("index", "序号", 52, "center"),
            ("status", "状态", 120, "center"),
            ("collection", "真实分类目录", 210, "w"),
            ("key", "Item Key", 95, "center"),
            ("type", "类型", 95, "center"),
            ("date", "日期", 110, "center"),
            ("publication", "期刊/来源", 180, "w"),
            ("pdf", "PDF数", 60, "center"),
            ("seconds", "耗时秒", 80, "center"),
            ("title", "标题", 500, "w"),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.pane.add(table_frame, minsize=260, stretch="always")

        self.rows: dict[str, str] = {}
        for task in tasks:
            item_id = self.tree.insert(
                "",
                "end",
                values=(
                    task.index,
                    "pending",
                    task.collection_path,
                    task.key,
                    task.item_type,
                    task.date,
                    task.publication,
                    str(task.pdf_count),
                    "",
                    task.title[:180],
                ),
            )
            self.rows[task.key] = item_id

        log_frame = ttk.Frame(self.pane)
        ttk.Label(log_frame, text="实时日志 / Live log", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=9, wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, pady=(4, 0))
        log_scroll.pack(side="right", fill="y", pady=(4, 0))
        self.pane.add(log_frame, minsize=170, height=210)
        self.log_text.tag_configure("cn", foreground="#1f2937")
        self.log_text.tag_configure("en", foreground="#64748b")
        self.log_text.tag_configure("warn", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

        if log_path.exists():
            self.apply_existing_progress(log_path)

        self.root.after(250, self.drain_events)
        self.root.after(1000, self.tick_clock)
        self.root.after(300, self.position_log_sash)
        self.update_stats()
        if auto_start:
            self.root.after(500, self.start)

    def log(self, message: str, *, level: str = "info") -> None:
        tag = "error" if level == "error" else "warn" if level == "warn" else "cn"
        for part_index, part in enumerate(str(message).splitlines()):
            line_tag = "en" if part.lstrip().startswith("EN:") else tag
            prefix = f"[{now_text()}] " if part_index == 0 else " " * 22
            self.log_text.insert("end", f"{prefix}{part}\n", line_tag)
        self.log_text.see("end")
        self.log_text.update_idletasks()

    def position_log_sash(self) -> None:
        height = max(self.root.winfo_height(), 660)
        self.pane.sash_place(0, 0, max(height - 245, 320))

    def apply_existing_progress(self, path: Path) -> None:
        records = read_jsonl(path)
        latest_by_key: dict[str, dict] = {}
        first_started_at: float | None = None
        for record in records:
            first_started_at = first_started_at or parse_record_time(record.get("time"))
            key = str(record.get("key") or "")
            if key:
                latest_by_key[key] = record
        if first_started_at:
            self.started_at = first_started_at

        for task in self.tasks:
            record = latest_by_key.get(task.key)
            if not record:
                continue
            status = str(record.get("status", ""))
            if status == "indexed":
                self.indexed += 1
                display_status = "新索引"
            elif status == "already_indexed":
                self.already += 1
                display_status = "已存在"
            elif status in {"failed", "error"}:
                self.failed += 1
                display_status = "失败"
            elif status in {"skipped", "unknown"}:
                self.skipped += 1
                display_status = "跳过"
            else:
                continue
            self.done += 1
            self.completed_keys.add(task.key)
            row = self.rows.get(task.key)
            if row:
                values = list(self.tree.item(row, "values"))
                values[1] = display_status
                values[8] = str(record.get("seconds", ""))
                self.tree.item(row, values=values)
        if self.done:
            self.current_title = f"已从进度日志恢复：{self.done}/{len(self.tasks)}"
            self.log(
                bilingual(
                    f"已读取既有进度日志，恢复完成 {self.done}/{len(self.tasks)}，后续仅处理未完成论文。",
                    f"Loaded existing progress: {self.done}/{len(self.tasks)} completed; "
                    "remaining papers only will be processed.",
                )
            )

    def update_stats(self) -> None:
        total = len(self.tasks)
        remaining = max(total - self.done, 0)
        percent = round(self.done * 100 / total, 1) if total else 0.0
        elapsed = time.time() - self.started_at if self.started_at else 0.0
        avg = elapsed / self.done if self.done else 0.0
        eta = avg * remaining if self.done else 0.0
        self.stats_var.set(
            f"已完成 {self.done}/{total} ({percent}%) | "
            f"新索引 {self.indexed} | 已存在 {self.already} | "
            f"跳过 {self.skipped} | 失败 {self.failed}"
        )
        self.progress["value"] = self.done
        self.total_var.set(str(total))
        self.done_detail_var.set(str(self.done))
        self.remaining_var.set(str(remaining))
        self.percent_var.set(f"{percent}%")
        self.elapsed_var.set(self.format_duration(elapsed))
        self.avg_var.set(self.format_duration(avg) if self.done else "等待首篇完成")
        self.eta_var.set(self.format_duration(eta) if self.done else "首篇完成后估算")
        self.current_var.set(self.current_title)

    @staticmethod
    def format_duration(seconds: float) -> str:
        seconds = max(int(seconds), 0)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}小时{minutes}分{secs}秒"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    def tick_clock(self) -> None:
        self.update_stats()
        if self.started and self.current_started_at and time.time() - self.last_heartbeat_at >= 5:
            self.last_heartbeat_at = time.time()
            current_elapsed = self.format_duration(time.time() - self.current_started_at)
            self.log(
                bilingual(
                    f"处理中：{self.current_title} | 本篇已处理 {current_elapsed}",
                    f"Running: {self.current_title} | current paper elapsed {current_elapsed}",
                )
            )
        self.root.after(1000, self.tick_clock)

    def start(self) -> None:
        if self.started:
            return
        if not self.tasks:
            messagebox.showwarning("No tasks", "No paper tasks found.")
            return
        self.started = True
        self.paused = False
        self.stop_requested.clear()
        self.started_at = self.started_at or time.time()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.pause_button.configure(state="normal")
        self.resume_button.configure(state="disabled")
        self.log(
            bilingual(
                f"开始语义全文索引任务。Vision={'开启' if self.enable_vision else '关闭'}。",
                f"Starting semantic full-text indexing. Vision={'ON' if self.enable_vision else 'OFF'}.",
            )
        )
        self.worker = threading.Thread(target=self.run_worker, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.paused = False
        self.stop_requested.set()
        self.stop_button.configure(state="disabled")
        self.pause_button.configure(state="disabled")
        self.log(
            bilingual(
                "已请求停止；当前论文完成后停止。",
                "Stop requested; the task will stop after the current paper finishes.",
            ),
            level="warn",
        )

    def pause(self) -> None:
        self.paused = True
        self.stop_requested.set()
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.log(
            bilingual(
                "已请求暂停；当前论文完成后暂停，进度已保存在 JSONL 和 ChromaDB 中。",
                "Pause requested; indexing will pause after the current paper, "
                "with progress saved in JSONL and ChromaDB.",
            ),
            level="warn",
        )

    def resume(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.paused = False
        self.stop_requested.clear()
        self.started = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.pause_button.configure(state="normal")
        self.resume_button.configure(state="disabled")
        self.log(
            bilingual(
                "继续索引：跳过已完成论文，从下一篇未完成论文接上。",
                "Resuming indexing: completed papers are skipped and the next unfinished paper will be processed.",
            )
        )
        self.worker = threading.Thread(target=self.run_worker, daemon=True)
        self.worker.start()

    def run_worker(self) -> None:
        for index, task in enumerate(self.tasks, start=1):
            if task.key in self.completed_keys:
                continue
            if self.stop_requested.is_set():
                self.events.put(("paused" if self.paused else "stopped", None))
                break
            self.events.put(("active", task.key))
            record = self.index_one(index, task)
            self.events.put(("result", record))
        else:
            self.events.put(("finished", None))

    def index_one(self, index: int, task: PaperTask) -> dict:
        command = [
            self.zotpilot_exe,
            "index",
            "--item-key",
            task.key,
            "--max-pages",
            str(self.max_pages),
            "--batch-size",
            "0",
        ]
        if not self.enable_vision:
            command.append("--no-vision")

        started = time.time()
        append_jsonl(
            self.log_path,
            {
                "time": now_text(),
                "status": "started",
                "index": index,
                "key": task.key,
                "title": task.title,
                "vision": self.enable_vision,
            },
        )
        self.events.put((
            "log",
            bilingual(
                f"[{index}/{len(self.tasks)}] 开始索引：{task.key} | {task.title}",
                f"[{index}/{len(self.tasks)}] Indexing: {task.key} | {task.title}",
            ),
        ))

        output_parts: list[str] = []
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            popen_kwargs = {
                "cwd": str(Path(__file__).resolve().parents[1]),
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(command, **popen_kwargs)
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                output_parts.append(line)
                if line:
                    runtime_warning = classify_runtime_warning(line, task.key)
                    if runtime_warning:
                        warning_id, warning_message = runtime_warning
                        self.events.put(("runtime_warning", (task.key, warning_id, warning_message)))
                    else:
                        self.events.put(("raw", line))
            returncode = process.wait()
        except Exception as exc:
            returncode = 999
            output_parts.append(f"{type(exc).__name__}: {exc}")

        elapsed = round(time.time() - started, 1)
        output = "\n".join(output_parts)
        status, message, counts = parse_index_result(output, returncode)
        record = {
            "time": now_text(),
            "status": status,
            "message": message,
            "returncode": returncode,
            "index": index,
            "key": task.key,
            "title": task.title,
            "pdf_count": task.pdf_count,
            "seconds": elapsed,
            "vision": self.enable_vision,
            "counts": counts,
        }
        if status in {"error", "failed", "unknown"}:
            record["output_tail"] = output_parts[-80:]
        append_jsonl(self.log_path, record)
        status_cn = {
            "indexed": "索引成功",
            "already_indexed": "已存在，增量跳过",
            "failed": "索引失败",
            "skipped": "跳过",
            "unknown": "状态未知",
            "error": "执行错误",
        }.get(status, status)
        self.events.put((
            "log",
            bilingual(
                f"{status_cn}：{task.key}，耗时 {elapsed} 秒，"
                f"Indexed={counts.get('indexed', 0)}，Already={counts.get('already_indexed', 0)}，"
                f"Failed={counts.get('failed', 0)}。",
                f"{status}: {task.key}, elapsed {elapsed}s, indexed={counts.get('indexed', 0)}, "
                f"already={counts.get('already_indexed', 0)}, failed={counts.get('failed', 0)}.",
            ),
        ))
        return record

    def drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "raw":
                    self.log(
                        bilingual(
                            f"ZotPilot 输出：{payload}",
                            f"ZotPilot output: {payload}",
                        )
                    )
                elif kind == "runtime_warning":
                    key, warning_id, warning_message = payload  # type: ignore[misc]
                    marker = (str(key), str(warning_id))
                    if marker not in self.runtime_warning_seen:
                        self.runtime_warning_seen.add(marker)
                        self.log(str(warning_message), level="warn")
                elif kind == "active":
                    key = str(payload)
                    task = next((item for item in self.tasks if item.key == key), None)
                    self.current_title = f"{key} | {task.title if task else ''}"
                    self.current_started_at = time.time()
                    row = self.rows.get(key)
                    if row:
                        current = list(self.tree.item(row, "values"))
                        current[1] = "处理中"
                        self.tree.item(row, values=current)
                        self.tree.selection_set(row)
                        self.tree.focus(row)
                        self.tree.see(row)
                    self.update_stats()
                elif kind == "result":
                    record = dict(payload)  # type: ignore[arg-type]
                    status = str(record["status"])
                    if status == "indexed":
                        self.indexed += 1
                        display_status = "新索引"
                    elif status == "already_indexed":
                        self.already += 1
                        display_status = "已存在"
                    elif status in {"skipped", "unknown"}:
                        self.skipped += 1
                        display_status = "跳过"
                    else:
                        self.failed += 1
                        display_status = "失败"
                    self.done += 1
                    self.completed_keys.add(str(record["key"]))
                    self.current_title = f"刚完成：{record['key']} | {record['title']}"
                    self.current_started_at = None
                    row = self.rows.get(str(record["key"]))
                    if row:
                        current = list(self.tree.item(row, "values"))
                        current[1] = display_status
                        current[8] = str(record.get("seconds", ""))
                        self.tree.item(row, values=current)
                    self.update_stats()
                elif kind == "paused":
                    self.current_started_at = None
                    self.stop_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled")
                    self.resume_button.configure(state="normal")
                    self.current_title = "已暂停，可点击继续索引"
                    self.log(
                        bilingual(
                            "索引已暂停；关闭窗口或电脑后，可用同一个进度日志继续。",
                            "Indexing is paused; after closing the window or shutting down, "
                            "resume with the same progress log.",
                        ),
                        level="warn",
                    )
                    self.update_stats()
                elif kind == "stopped":
                    self.current_started_at = None
                    self.stop_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled")
                    self.resume_button.configure(state="normal")
                    self.current_title = "已停止，可点击继续索引"
                    self.log(
                        bilingual(
                            "索引已停止；进度已保留，可点击继续索引。",
                            "Indexing stopped; progress is preserved and can be resumed.",
                        ),
                        level="warn",
                    )
                    self.update_stats()
                elif kind == "finished":
                    self.stop_button.configure(state="disabled")
                    self.pause_button.configure(state="disabled")
                    self.resume_button.configure(state="disabled")
                    self.current_title = "任务已完成"
                    self.log(
                        bilingual(
                            "任务已完成。",
                            "Task finished.",
                        )
                    )
                    self.update_stats()
        except queue.Empty:
            pass
        self.root.after(250, self.drain_events)


def main() -> None:
    parser = argparse.ArgumentParser(description="ZotPilot collection semantic index GUI")
    parser.add_argument("--keys", type=Path, default=default_keys_path(), help="JSON task list exported from Zotero")
    parser.add_argument("--index-root", type=Path, default=default_index_root(), help="ZotPilot index root directory")
    parser.add_argument("--zotpilot-exe", type=Path, default=None, help="Path to zotpilot executable")
    parser.add_argument("--limit", type=int, default=2, help="Index first N PDF-backed papers; 0 means all")
    parser.add_argument("--max-pages", type=int, default=0, help="0 means no page limit")
    parser.add_argument("--no-vision", action="store_true", help="Disable ZotPilot vision extraction")
    parser.add_argument("--include-no-pdf", action="store_true")
    parser.add_argument("--auto-start", action="store_true")
    parser.add_argument(
        "--resume-log",
        type=Path,
        default=None,
        help="Reuse an existing JSONL progress log and skip completed keys",
    )
    args = parser.parse_args()

    if args.keys is None:
        raise SystemExit(
            f"Missing --keys. Pass a task JSON path or set {DEFAULT_KEYS_ENV} to one."
        )
    if not args.keys.exists():
        raise SystemExit(f"Task JSON not found: {args.keys}")

    tasks = load_tasks(args.keys, include_no_pdf=args.include_no_pdf)
    if args.limit and args.limit > 0:
        tasks = tasks[: args.limit]

    log_dir = args.index_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if args.resume_log:
        log_path = args.resume_log
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / f"zotpilot_index_progress_{stamp}.jsonl"

    root = tk.Tk()
    ZotPilotIndexWindow(
        root,
        tasks,
        log_path=log_path,
        index_root=args.index_root,
        zotpilot_exe=resolve_zotpilot_exe(args.zotpilot_exe),
        max_pages=args.max_pages,
        enable_vision=not args.no_vision,
        auto_start=args.auto_start,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
