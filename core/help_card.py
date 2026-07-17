from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .renderer import render_html_template

HELP_CARD_CANVAS_WIDTH = 900
HELP_CARD_WIDTH = 868
HELP_CARD_BODY_PADDING = 16
HELP_CARD_RENDER_OPTIONS: dict[str, Any] = {
    "full_page": True,
    "type": "png",
    "quality": 90,
    "timeout": 5000,
    "viewport_width": HELP_CARD_CANVAS_WIDTH,
    "viewport_height": 1600,
    "device_scale_factor_level": "high",
    "wait_until": "domcontentloaded",
}

PUBLIC_HELP_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "number": "01",
        "tone": "mint",
        "title": "快速开始与状态",
        "description": "先确认插件状态，再进入创建或管理流程。",
        "commands": (
            {
                "syntax": "status",
                "description": "查看服务状态、Endpoint 数量与渲染模式",
            },
            {
                "syntax": "help",
                "description": "再次打开帮助；中文别名为“帮助”",
            },
        ),
    },
    {
        "number": "02",
        "tone": "blue",
        "title": "创建与验证",
        "description": "创建操作从私聊发起；群聊 Endpoint 需要在目标群完成验证。",
        "commands": (
            {
                "syntax": "token new private [名称]",
                "description": "创建私聊 Endpoint；名称可选",
            },
            {
                "syntax": "token new group <群号> [名称]",
                "description": "申请群聊 Endpoint；请先在私聊执行",
            },
            {
                "syntax": "token verify <request_id> <code>",
                "description": "在目标群完成验证；需群主或群管理员",
            },
        ),
    },
    {
        "number": "03",
        "tone": "violet",
        "title": "管理自己的 Endpoint",
        "description": "列表、轮换和撤销均按当前用户隔离，建议在私聊中操作。",
        "commands": (
            {
                "syntax": "token list",
                "description": "查看自己的可用 Endpoint",
            },
            {
                "syntax": "token rotate <名称>",
                "description": "轮换凭据并立即停用旧凭据",
            },
            {
                "syntax": "token revoke <名称>",
                "description": "撤销自己的 Endpoint",
            },
        ),
    },
)

ADMIN_HELP_SECTION: dict[str, Any] = {
    "number": "04",
    "tone": "amber",
    "title": "管理员工具",
    "description": "仅 AstrBot 超级管理员 · 仅私聊",
    "commands": (
        {
            "syntax": "admin token list",
            "description": "查看 Registry 中的 Endpoint",
        },
        {
            "syntax": "admin token revoke-path <endpoint-path>",
            "description": "按完整 path 精确撤销",
        },
        {
            "syntax": "admin token revoke-owner <owner_user_id> <名称>",
            "description": "按 owner 与名称精确撤销",
        },
    ),
}


@lru_cache(maxsize=1)
def _load_help_card_template() -> str:
    template_path = Path(__file__).resolve().parents[1] / "templates" / "help_card.html"
    return template_path.read_text(encoding="utf-8")


def render_help_card_html(is_admin: bool) -> str:
    """渲染插件内置帮助卡片，不读取或占用 active 通知模板。"""
    event = {
        "sections": PUBLIC_HELP_SECTIONS,
        "admin_section": ADMIN_HELP_SECTION if is_admin else None,
    }
    return render_html_template(_load_help_card_template(), event)


def build_help_text(is_admin: bool) -> str:
    """构造卡片失败时使用的结构化纯文本帮助。"""
    lines = [
        "Webhook Notifier 命令帮助",
        "在当前 AstrBot 唤醒词后输入 whn 和以下子命令。",
    ]
    sections = list(PUBLIC_HELP_SECTIONS)
    if is_admin:
        sections.append(ADMIN_HELP_SECTION)

    for section in sections:
        lines.extend(("", f"【{section['title']}】"))
        if section is ADMIN_HELP_SECTION:
            lines.append(str(section["description"]))
        for command in section["commands"]:
            lines.append(f"- {command['syntax']}：{command['description']}")

    lines.extend(("", "参数提示：[ ] 为可选参数，< > 为必填参数。"))
    return "\n".join(lines)
