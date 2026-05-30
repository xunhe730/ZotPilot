from __future__ import annotations

import argparse
import json
import os
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

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


def latest_log(log_dir: Path) -> Path | None:
    logs = sorted(log_dir.glob("zotpilot_index_progress_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


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


def fmt_seconds(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def parse_record_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


class MonitorWindow:
    def __init__(
        self,
        root: tk.Tk,
        keys_path: Path,
        log_path: Path | None,
        index_root: Path,
        interval_ms: int,
    ) -> None:
        self.root = root
        self.keys_path = keys_path
        self.log_path = log_path
        self.index_root = index_root
        self.log_dir = index_root / "logs"
        self.interval_ms = interval_ms
        self.tasks = json.loads(keys_path.read_text(encoding="utf-8"))
        self.pdf_tasks = [item for item in self.tasks if int(item.get("pdf_count") or 0) > 0]
        self.started_at: datetime | None = None
        self._seen_record_count = 0

        root.title("zotero_mcp_zotpilot 语义索引增强监控")
        root.geometry("1720x900")
        root.minsize(1180, 660)

        title = ttk.Label(
            root,
            text="zotero_mcp_zotpilot 语义索引增强监控",
            font=("Segoe UI", 15, "bold"),
        )
        title.pack(anchor="w", padx=16, pady=(12, 4))
        self.info_var = tk.StringVar(value=f"索引目录：{index_root} | max_pages=0 | Vision ON")
        ttk.Label(root, textvariable=self.info_var, foreground="#555").pack(anchor="w", padx=16)
        self.path_var = tk.StringVar()
        ttk.Label(root, textvariable=self.path_var, foreground="#555").pack(anchor="w", padx=16)

        controls = ttk.Frame(root)
        controls.pack(fill="x", padx=16, pady=(8, 8))
        self.monitor_button = ttk.Button(controls, text="监控中", state="disabled")
        self.monitor_button.pack(side="left")
        self.executor_note_button = ttk.Button(controls, text="停止请用原执行窗口", state="disabled")
        self.executor_note_button.pack(side="left", padx=(8, 0))
        self.quick_stats_var = tk.StringVar(value="读取进度中")
        ttk.Label(controls, textvariable=self.quick_stats_var, font=("Segoe UI", 10)).pack(side="left", padx=16)

        stats = ttk.LabelFrame(root, text="任务统计")
        stats.pack(fill="x", padx=16, pady=10)
        self.total_var = tk.StringVar()
        self.done_var = tk.StringVar()
        self.remaining_var = tk.StringVar()
        self.percent_var = tk.StringVar()
        self.elapsed_var = tk.StringVar()
        self.avg_var = tk.StringVar()
        self.eta_var = tk.StringVar()
        self.current_var = tk.StringVar()
        stat_items = [
            ("总文件数", self.total_var),
            ("已完成", self.done_var),
            ("剩余文件数", self.remaining_var),
            ("完成百分比", self.percent_var),
            ("已用时间", self.elapsed_var),
            ("平均每篇", self.avg_var),
            ("预计剩余时间", self.eta_var),
        ]
        for idx, (label, variable) in enumerate(stat_items):
            ttk.Label(stats, text=f"{label}：", foreground="#555").grid(
                row=idx // 4, column=(idx % 4) * 2, sticky="e", padx=(10, 2), pady=4
            )
            ttk.Label(stats, textvariable=variable, font=("Segoe UI", 10, "bold")).grid(
                row=idx // 4, column=(idx % 4) * 2 + 1, sticky="w", padx=(0, 18), pady=4
            )
        ttk.Label(stats, text="当前论文：", foreground="#555").grid(row=2, column=0, sticky="ne", padx=(10, 2), pady=4)
        ttk.Label(stats, textvariable=self.current_var, wraplength=1500).grid(
            row=2, column=1, columnspan=7, sticky="w", padx=(0, 10), pady=4
        )

        self.progress = ttk.Progressbar(root, maximum=max(len(self.pdf_tasks), 1), mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(0, 10))

        self.pane = ttk.PanedWindow(root, orient=tk.VERTICAL)
        self.pane.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        table_frame = ttk.Frame(self.pane)
        columns = ("index", "status", "collection", "key", "type", "date", "publication", "pdf", "seconds", "title")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)
        specs = [
            ("index", "序号", 50, "center"),
            ("status", "状态", 90, "center"),
            ("collection", "真实分类目录", 230, "w"),
            ("key", "Item Key", 95, "center"),
            ("type", "类型", 95, "center"),
            ("date", "日期", 120, "center"),
            ("publication", "期刊/来源", 220, "w"),
            ("pdf", "PDF数", 55, "center"),
            ("seconds", "耗时秒", 75, "center"),
            ("title", "标题", 640, "w"),
        ]
        for col, label, width, anchor in specs:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor)
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.pane.add(table_frame, weight=4)

        log_frame = ttk.Frame(self.pane)
        ttk.Label(log_frame, text="实时日志 / Live log", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=8, wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, pady=(4, 0))
        log_scroll.pack(side="right", fill="y", pady=(4, 0))
        self.log_text.tag_configure("cn", foreground="#1f2937")
        self.log_text.tag_configure("en", foreground="#64748b")
        self.pane.add(log_frame, weight=1)

        self.rows: dict[str, str] = {}
        for item in self.pdf_tasks:
            iid = self.tree.insert("", "end", values=self.row_values(item, "pending", ""))
            self.rows[item["key"]] = iid
        self.refresh()

    def row_values(self, item: dict, status: str, seconds: str) -> tuple:
        return (
            item.get("index", ""),
            status,
            item.get("collection_path", ""),
            item.get("key", ""),
            item.get("type", ""),
            item.get("date", ""),
            item.get("publication", ""),
            item.get("pdf_count", ""),
            seconds,
            str(item.get("title", ""))[:220],
        )

    def log(self, cn: str, en: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {cn}\n", "cn")
        self.log_text.insert("end", f"                      EN: {en}\n", "en")
        self.log_text.see("end")

    def refresh(self) -> None:
        if self.log_path is None or not self.log_path.exists():
            self.log_path = latest_log(self.log_dir)
        records = read_jsonl(self.log_path) if self.log_path else []
        self.path_var.set(f"进度日志：{self.log_path or '未找到'}")

        status_by_key: dict[str, dict] = {}
        current: dict | None = None
        for rec in records:
            key = rec.get("key")
            if not key:
                continue
            if rec.get("status") == "started":
                status_by_key[key] = rec
                current = rec
                self.started_at = self.started_at or parse_record_time(rec.get("time"))
            else:
                status_by_key[key] = rec
                current = None
                self.started_at = self.started_at or parse_record_time(rec.get("time"))

        done = 0
        failed = 0
        indexed = 0
        already = 0
        total_seconds = 0.0
        for item in self.pdf_tasks:
            key = item["key"]
            rec = status_by_key.get(key, {})
            raw_status = rec.get("status", "pending")
            seconds = rec.get("seconds", "")
            if raw_status == "indexed":
                status = "新索引"
                indexed += 1
                done += 1
                total_seconds += float(seconds or 0)
            elif raw_status == "already_indexed":
                status = "已存在"
                already += 1
                done += 1
                total_seconds += float(seconds or 0)
            elif raw_status == "started":
                status = "处理中"
            elif raw_status in {"failed", "error"}:
                status = "失败"
                failed += 1
                done += 1
                total_seconds += float(seconds or 0)
            else:
                status = "等待"
            iid = self.rows.get(key)
            if iid:
                self.tree.item(iid, values=self.row_values(item, status, str(seconds)))
                if status == "处理中":
                    self.tree.selection_set(iid)
                    self.tree.focus(iid)
                    self.tree.see(iid)

        total = len(self.pdf_tasks)
        remaining = max(total - done, 0)
        percent = done * 100 / total if total else 0
        avg = total_seconds / done if done else 0
        eta = avg * remaining if done else 0
        elapsed = (datetime.now() - self.started_at).total_seconds() if self.started_at else 0
        self.progress["value"] = done
        self.quick_stats_var.set(
            f"已完成 {done}/{total} ({percent:.1f}%) | 新索引 {indexed} | 已存在 {already} | 跳过 0 | 失败 {failed}"
        )
        self.total_var.set(str(total))
        self.done_var.set(f"{done}（新索引 {indexed} / 已存在 {already} / 失败 {failed}）")
        self.remaining_var.set(str(remaining))
        self.percent_var.set(f"{percent:.1f}%")
        self.elapsed_var.set(fmt_seconds(elapsed))
        self.avg_var.set(fmt_seconds(avg))
        self.eta_var.set(fmt_seconds(eta))
        if current:
            started_time = parse_record_time(current.get("time"))
            running_text = ""
            if started_time:
                running_text = f" | 本篇已处理 {fmt_seconds((datetime.now() - started_time).total_seconds())}"
            self.current_var.set(f"{current.get('key')} | {current.get('title')}{running_text}")
        else:
            self.current_var.set("等待下一条或任务已完成")

        if records and len(records) != self._seen_record_count:
            last = records[-1]
            self.log(
                f"刷新：最后记录 {last.get('status')} / {last.get('key')}，完成 {done}/{total}。",
                f"Refresh: last record {last.get('status')} / {last.get('key')}; done {done}/{total}.",
            )
            self._seen_record_count = len(records)
        elif current:
            now = datetime.now()
            started_time = parse_record_time(current.get("time"))
            running_seconds = (now - started_time).total_seconds() if started_time else 0
            self.log(
                f"处理中：{current.get('key')}，本篇已运行 {fmt_seconds(running_seconds)}，总进度 {done}/{total}。",
                f"Running: {current.get('key')} for {fmt_seconds(running_seconds)}; progress {done}/{total}.",
            )
        self.root.after(self.interval_ms, self.refresh)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", type=Path, default=default_keys_path())
    parser.add_argument("--index-root", type=Path, default=default_index_root())
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--interval-ms", type=int, default=5000)
    args = parser.parse_args()
    if args.keys is None:
        raise SystemExit(
            f"Missing --keys. Pass a task JSON path or set {DEFAULT_KEYS_ENV} to one."
        )
    if not args.keys.exists():
        raise SystemExit(f"Task JSON not found: {args.keys}")
    root = tk.Tk()
    MonitorWindow(root, args.keys, args.log or latest_log(args.index_root / "logs"), args.index_root, args.interval_ms)
    root.mainloop()


if __name__ == "__main__":
    main()
