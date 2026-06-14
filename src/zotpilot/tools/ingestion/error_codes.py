"""Centralized error / warning code → user-facing Chinese guidance.

Single source of truth for ingest error and warning messaging, so the same
code is not translated in three different places (P1-D). See
docs/plan-connector-download-enhancement.md (工作流 D ①).

Two channels:
- **action_required** carries *blocking* codes (anti_bot_detected, ...): the
  skill STOPs and gates on them.
- **notices** carries *informational* codes (pdf_attention): the skill renders
  them under the results table but NEVER STOPs or asks Y/N — this preserves the
  "never combine the Phase 2 retry gate and the Phase 3 Y/N gate" invariant
  (ztp-research.md gate semantics).
"""

from __future__ import annotations

# code -> {"zh": short label, "next_steps_zh": actionable guidance, "rescuable": bool}
ERROR_CODE_DICT: dict[str, dict[str, object]] = {
    "anti_bot_detected": {
        "zh": "反爬验证页",
        "next_steps_zh": ("页面被反爬拦截。请在浏览器中打开该链接完成 CAPTCHA / 登录 / 确认，然后回复 Y 重试该篇。"),
        "rescuable": True,
    },
    "pdf_antibot_blocked": {
        "zh": "PDF 抓取被二次反爬拦截",
        "next_steps_zh": (
            "元数据已入库，但 PDF 在抓取阶段被出版商二次反爬拦截。"
            "请在浏览器中手动打开这篇的 PDF 一次（养热会话），"
            "或在 Zotero 右键条目 'Find Available PDF'，然后单篇重试——不要整批重试。"
        ),
        "rescuable": True,
    },
    "pdf_not_attached": {
        "zh": "PDF 未挂载",
        "next_steps_zh": (
            "元数据已入库，但未拿到 PDF。若 Zotero 仍弹 translator 对话框"
            "（如 Elsevier 'Continue'），点一下完成即可；不要对该 DOI 重新入库，"
            "否则会产生重复条目。"
        ),
        "rescuable": True,
    },
    "oa_quota_exceeded": {
        "zh": "Zotero 云存储配额已满",
        "next_steps_zh": (
            "OA PDF 下载因 Zotero 云存储配额已满而中止。请清理云配额，"
            "或在 Zotero 右键条目 'Find Available PDF' 挂到本地存储（WebDAV / 本地）。"
        ),
        "rescuable": True,
    },
    "no_translator": {
        "zh": "无匹配的 Zotero 转译器",
        "next_steps_zh": "该页面没有匹配的 Zotero 转译器，已自动改用 DOI 元数据入库。",
        "rescuable": True,
    },
    "completion_unconfirmed": {
        "zh": "入库确认超时",
        "next_steps_zh": ("入库已触发但确认超时；请在 Zotero 库中检查该条目，若未找到回复 retry 重试。"),
        "rescuable": True,
    },
    "connector_offline": {
        "zh": "Connector 未连接",
        "next_steps_zh": ("ZotPilot 浏览器扩展未连接。请确认 Chrome 已打开、扩展已启用，然后重试。"),
        "rescuable": True,
    },
}

# Informational warning codes that bubble to `notices` (display-only, never gate).
NOTICE_CODES: frozenset[str] = frozenset({"pdf_antibot_blocked", "pdf_not_attached", "oa_quota_exceeded"})


def next_steps_zh(code: str, default: str = "") -> str:
    """Return the actionable Chinese guidance for a code, or ``default``."""
    entry = ERROR_CODE_DICT.get(code)
    if entry is None:
        return default
    return str(entry["next_steps_zh"])


def pdf_attention_entry(row: dict) -> dict | None:
    """Build a display-only ``pdf_attention`` notice from a saved-with-warning row.

    Returns ``None`` unless the row carries a recognized informational
    ``warning_code``. These notices are surfaced under the results table; they
    NEVER trigger a Phase-2 STOP / Y-N gate.
    """
    code = row.get("warning_code")
    if code not in NOTICE_CODES:
        return None
    return {
        "type": "pdf_attention",
        "code": code,
        "message": next_steps_zh(str(code), str(row.get("warning") or "")),
        "identifier": row.get("identifier", ""),
        "item_key": row.get("item_key"),
        "title": row.get("title", ""),
    }
