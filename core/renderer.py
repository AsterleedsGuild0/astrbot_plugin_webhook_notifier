from __future__ import annotations

import os
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from jinja2 import BaseLoader
from jinja2.sandbox import SandboxedEnvironment
from jinja2.utils import Namespace
from markupsafe import Markup

from .models import DisplayContext, NormalizedEvent
from .display import build_display_event_data, format_duration_ms, format_timestamp

DEFAULT_FALLBACK_VIEWPORT_WIDTH = 1280
DEVICE_SCALE_CANDIDATES = (1.0, 1.3, 1.8)
HTML_BODY_PADDING = 16
HTML_CARD_WIDTH = 780
RIGHT_VISUAL_CROP_PADDING = 12
MAX_RENDERED_HTML_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_EVENT_BYTES = 64 * 1024
MAX_PREVIEW_DEPTH = 10
MAX_PREVIEW_NODES = 2000
MAX_PREVIEW_CONTAINER = 200
MAX_PREVIEW_STRING = 8192
SUBAGENT_TIMELINE_MISSING_RATIO_LIMIT = 0.25
SUBAGENT_TIMELINE_SIMPLE_BASE_ITEMS = 5
SUBAGENT_TIMELINE_SIMPLE_MAX_DEPTH = 2
SUBAGENT_TIMELINE_SIMPLE_MAX_CONCURRENCY = 2
SUBAGENT_TIMELINE_COMPLEXITY_THRESHOLD = 4
SUBAGENT_TIMELINE_MAIN_ITEM_LIMIT = 8
SUBAGENT_TIMELINE_MAX_ITEMS = 64
SUBAGENT_TIMELINE_MIN_VIEWPORT_WIDTH = 1440
SUBAGENT_TIMELINE_MAX_VIEWPORT_WIDTH = 2400
SUBAGENT_TIMELINE_MIN_VIEWPORT_HEIGHT = 1200
SUBAGENT_TIMELINE_MAX_VIEWPORT_HEIGHT = 8192
SUBAGENT_TIMELINE_BODY_PADDING = 16
SUBAGENT_TIMELINE_INNER_PADDING = 32
SUBAGENT_TIMELINE_BORDER_WIDTH = 1
SUBAGENT_TIMELINE_STATE_COLUMN_WIDTH = 136
SUBAGENT_TIMELINE_MIN_NAME_COLUMN_WIDTH = 320
SUBAGENT_TIMELINE_MAX_NAME_COLUMN_WIDTH = 560
SUBAGENT_TIMELINE_MIN_PLOT_WIDTH = 880
SUBAGENT_TIMELINE_MAX_PLOT_WIDTH = 1680
SUBAGENT_TIMELINE_MIN_BAR_WIDTH = 8
SUBAGENT_TIMELINE_SOFT_HEIGHT = 3000
SUBAGENT_TIMELINE_MAX_TIMEOUT_MS = 15000
CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
)
_SENSITIVE_KEYS = {
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "apikey",
    "accesstoken",
}
_FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "base", "form"}
_CSS_DANGEROUS = re.compile(r"url\s*\(|@import\b|expression\s*\(", re.I)
_EXTERNAL_RESOURCE = re.compile(r"(?:https?|file)\s*:", re.I)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\r\n]+)`(?!`)")
_TIMELINE_REF_LIKE_RE = re.compile(r"^[a-f0-9]{32,64}$", re.I)
_TIMELINE_WINDOWS_PATH_RE = re.compile(r"^[a-z]:[\\/]", re.I)
_TIMELINE_STATUSES = ("completed", "failed", "running", "cancelled", "unknown")
_TIMELINE_STATUS_VIEW = {
    "completed": ("已完成", "✓"),
    "failed": ("失败", "!"),
    "running": ("进行中", "…"),
    "cancelled": ("已取消", "×"),
    "unknown": ("状态未知", "?"),
}
_TIMELINE_DENSITY_VIEW = {
    "comfortable": {
        "row_min_height": 54,
        "task_font_size": 15,
        "task_line_height": 1.36,
        "task_padding_y": 8,
        "bar_height": 20,
        "axis_height": 48,
        "agent_font_size": 11,
        "show_agent": True,
        "base_timeout_ms": 8000,
    },
    "compact": {
        "row_min_height": 42,
        "task_font_size": 14,
        "task_line_height": 1.33,
        "task_padding_y": 6,
        "bar_height": 18,
        "axis_height": 44,
        "agent_font_size": 10,
        "show_agent": True,
        "base_timeout_ms": 10000,
    },
    "dense": {
        "row_min_height": 36,
        "task_font_size": 13,
        "task_line_height": 1.30,
        "task_padding_y": 4,
        "bar_height": 16,
        "axis_height": 42,
        "agent_font_size": 10,
        "show_agent": False,
        "base_timeout_ms": 12000,
    },
}


@dataclass(frozen=True)
class SubagentTimelineLayout:
    """Deterministic render metadata shared by the template and T2I caller."""

    viewport_width: int
    card_width: int
    name_column_width: int
    plot_width: int
    state_column_width: int
    density: str
    row_min_height: int
    task_font_size: int
    task_line_height: float
    task_padding_y: int
    bar_height: int
    axis_height: int
    tick_target_count: int
    located_rows_height: int
    unlocated_rows_height: int
    vertical_chrome_height: int
    estimated_height: int
    viewport_height: int
    soft_height_exceeded: bool
    render_timeout_ms: int
    prefer_normal_scale: bool
    body_padding: int = SUBAGENT_TIMELINE_BODY_PADDING

    def to_view(self) -> dict[str, Any]:
        return {
            "viewport_width": self.viewport_width,
            "card_width": self.card_width,
            "name_column_width": self.name_column_width,
            "plot_width": self.plot_width,
            "state_column_width": self.state_column_width,
            "density": self.density,
            "row_min_height": self.row_min_height,
            "task_font_size": self.task_font_size,
            "task_line_height": self.task_line_height,
            "task_padding_y": self.task_padding_y,
            "bar_height": self.bar_height,
            "axis_height": self.axis_height,
            "tick_target_count": self.tick_target_count,
            "located_rows_height": self.located_rows_height,
            "unlocated_rows_height": self.unlocated_rows_height,
            "vertical_chrome_height": self.vertical_chrome_height,
            "estimated_height": self.estimated_height,
            "viewport_height": self.viewport_height,
            "soft_height_exceeded": self.soft_height_exceeded,
            "render_timeout_ms": self.render_timeout_ms,
            "prefer_normal_scale": self.prefer_normal_scale,
            "body_padding": self.body_padding,
        }

    @classmethod
    def from_view(cls, value: dict[str, Any]) -> SubagentTimelineLayout:
        return cls(**value)


@dataclass(frozen=True)
class RenderedSubagentTimeline:
    html: str
    layout: SubagentTimelineLayout


class PreparedSubagentTimeline:
    """Request-local lazy cache so main and attachment rendering share one view."""

    def __init__(self, event: NormalizedEvent) -> None:
        self._event = event
        self._resolved = False
        self._view: dict[str, Any] | None = None

    @property
    def view(self) -> dict[str, Any] | None:
        if not self._resolved:
            self._view = _build_subagent_timeline_view(self._event)
            self._resolved = True
        return self._view


# 默认文本模板（与 FSD 一致）
DEFAULT_TEXT_TEMPLATE = """\
[{{ event.source.name }}] {{ event.title }}

状态：{{ event.status_display }}

{% if event.summary %}{{ event.summary }}
{% endif %}{% for field in event.fields %}
{{ field.label }}：{{ field.value }}{% endfor %}
"""

# 默认 HTML 卡片模板，由 designer 设计。
# 自包含、无外部资源，使用 Jinja2 模板语法。
# 上下文根变量为 event，其值由 render_html_data() 生成的 dict 提供。
DEFAULT_HTML_TEMPLATE = """\
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      padding: 0;
      width: fit-content;
      min-width: 0;
      min-height: 0;
      height: auto;
      color: #1d1d1f;
      background: #ffffff;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }

    body {
      padding: 16px;
      display: block;
      overflow-x: hidden;
    }

    .card {
      position: relative;
      width: 780px;
      max-width: 780px;
      overflow: hidden;
      border: 1px solid rgba(0, 0, 0, 0.10);
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(250, 250, 252, 0.94));
      box-shadow: 0 18px 46px rgba(0, 0, 0, 0.12), 0 1px 0 rgba(255, 255, 255, 0.80) inset;
    }

    .card-inner {
      position: relative;
      padding: 28px 32px 24px;
    }

    .ambient-line {
      position: absolute;
      left: 0;
      right: 0;
      top: 0;
      height: 1px;
      background: rgba(255, 255, 255, 0.88);
    }

    .topbar {
      display: table;
      width: 100%;
      margin-bottom: 18px;
    }

    .source-wrap,
    .status-wrap {
      display: table-cell;
      vertical-align: top;
    }

    .status-wrap {
      text-align: right;
    }

    .eyebrow {
      display: inline-block;
      max-width: 520px;
      padding: 5px 10px;
      border: 1px solid rgba(0, 0, 0, 0.08);
      border-radius: 999px;
      color: #6e6e73;
      background: rgba(255, 255, 255, 0.72);
      font-size: 17px;
      font-weight: 500;
      line-height: 1.3;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .status-badge {
      position: relative;
      display: inline-block;
      min-width: 0;
      padding: 5px 11px 5px 24px;
      border: 1px solid rgba(0, 0, 0, 0.08);
      border-radius: 999px;
      color: #4f5866;
      background: rgba(255, 255, 255, 0.78);
      font-size: 17px;
      font-weight: 600;
      line-height: 1.3;
      text-align: center;
      overflow-wrap: anywhere;
    }

    .status-badge:before {
      content: "";
      position: absolute;
      left: 10px;
      top: 50%;
      width: 8px;
      height: 8px;
      margin-top: -4px;
      border-radius: 50%;
      background: #8e8e93;
      box-shadow: 0 0 0 2px rgba(142, 142, 147, 0.12);
    }

    .status-success {
      color: #24663f;
      background: rgba(52, 199, 89, 0.11);
      border-color: rgba(52, 199, 89, 0.22);
    }

    .status-success:before {
      background: #34c759;
      box-shadow: 0 0 0 2px rgba(52, 199, 89, 0.14);
    }

    .status-error {
      color: #9f2d2f;
      background: rgba(255, 59, 48, 0.10);
      border-color: rgba(255, 59, 48, 0.22);
    }

    .status-error:before {
      background: #ff3b30;
      box-shadow: 0 0 0 2px rgba(255, 59, 48, 0.13);
    }

    .status-warning {
      color: #8a5a00;
      background: rgba(255, 204, 0, 0.14);
      border-color: rgba(255, 204, 0, 0.30);
    }

    .status-warning:before {
      background: #ffcc00;
      box-shadow: 0 0 0 2px rgba(255, 204, 0, 0.16);
    }

    h1 {
      margin: 0;
      color: #1d1d1f;
      font-size: 34px;
      font-weight: 700;
      line-height: 1.18;
      letter-spacing: -0.02em;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .summary {
      margin-top: 14px;
      padding: 14px 16px;
      border: 1px solid rgba(0, 0, 0, 0.07);
      border-radius: 14px;
      color: #3a3a3c;
      background: rgba(255, 255, 255, 0.64);
      font-size: 21px;
      line-height: 1.48;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .summary code,
    .field-value code {
      padding: 0.08em 0.34em;
      border: 1px solid rgba(60, 60, 67, 0.14);
      border-radius: 6px;
      color: #34343a;
      background: rgba(118, 118, 128, 0.10);
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", "PingFang SC", monospace;
      font-size: 0.88em;
      line-height: inherit;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-all;
      -webkit-box-decoration-break: clone;
      box-decoration-break: clone;
    }

    .section-label {
      margin: 22px 0 8px;
      color: #8a8a8e;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.12em;
    }

    .fields {
      margin: 0;
      padding: 0;
      list-style: none;
      border: 1px solid rgba(0, 0, 0, 0.08);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.74);
      overflow: hidden;
    }

    .field {
      display: table;
      width: 100%;
      border-top: 1px solid rgba(60, 60, 67, 0.12);
    }

    .field:first-child {
      border-top: 0;
    }

    .field-name,
    .field-value {
      display: table-cell;
      vertical-align: top;
      padding: 12px 16px;
      font-size: 19px;
      line-height: 1.42;
    }

    .field-name {
      width: 190px;
      color: #6e6e73;
      font-weight: 600;
      background: rgba(245, 245, 247, 0.55);
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .field-name-secondary,
    .field-value-secondary {
      color: #8a8a8e;
      font-size: 16px;
      font-weight: 500;
    }

    .field-value {
      color: #1d1d1f;
      overflow-wrap: anywhere;
      word-break: break-word;
      white-space: pre-wrap;
    }

    .empty-fields {
      padding: 14px 16px;
      color: #8a8a8e;
      font-size: 18px;
      line-height: 1.45;
    }

    .subagent-panel {
      overflow: hidden;
      border: 1px solid rgba(0, 0, 0, 0.08);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.74);
    }

    .subagent-summary {
      padding: 14px 16px;
      border-bottom: 1px solid rgba(60, 60, 67, 0.12);
      background: rgba(245, 245, 247, 0.52);
    }

    .subagent-summary-main {
      color: #1d1d1f;
      font-size: 18px;
      font-weight: 700;
      line-height: 1.4;
    }

    .subagent-summary-meta {
      margin-top: 4px;
      color: #6e6e73;
      font-size: 15px;
      line-height: 1.45;
    }

    .subagent-flags {
      margin-top: 8px;
    }

    .subagent-flag,
    .subagent-status,
    .subagent-parallel {
      display: inline-block;
      border-radius: 999px;
      font-weight: 700;
      line-height: 1.25;
    }

    .subagent-flag {
      margin: 0 6px 4px 0;
      padding: 3px 8px;
      border: 1px solid rgba(142, 142, 147, 0.20);
      color: #6e6e73;
      background: rgba(255, 255, 255, 0.78);
      font-size: 13px;
    }

    .subagent-notice {
      margin: 12px 14px 0;
      padding: 10px 12px;
      border: 1px dashed rgba(142, 142, 147, 0.34);
      border-radius: 11px;
      color: #5f6065;
      background: rgba(245, 245, 247, 0.56);
      font-size: 14px;
      line-height: 1.45;
    }

    .subagent-list {
      margin: 0;
      padding: 8px 14px 12px;
      list-style: none;
    }

    .subagent-item {
      position: relative;
      margin-top: 8px;
      padding: 11px 12px;
      border: 1px solid rgba(60, 60, 67, 0.13);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.88);
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.025);
    }

    .subagent-item.depth-2,
    .subagent-item.depth-3,
    .subagent-item.depth-4 {
      border-left-width: 3px;
      border-left-color: rgba(65, 108, 157, 0.42);
    }

    .subagent-item.depth-2 {
      margin-left: 18px;
    }

    .subagent-item.depth-3 {
      margin-left: 36px;
    }

    .subagent-item.depth-4 {
      margin-left: 54px;
    }

    .subagent-item.depth-2:before,
    .subagent-item.depth-3:before,
    .subagent-item.depth-4:before {
      content: "";
      position: absolute;
      top: 50%;
      right: 100%;
      width: 14px;
      border-top: 1px solid rgba(65, 108, 157, 0.34);
    }

    .subagent-item-head {
      display: table;
      width: 100%;
    }

    .subagent-name-wrap,
    .subagent-state-wrap {
      display: table-cell;
      vertical-align: top;
    }

    .subagent-state-wrap {
      width: 1%;
      padding-left: 12px;
      white-space: nowrap;
      text-align: right;
    }

    .subagent-name {
      color: #1d1d1f;
      font-size: 17px;
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .subagent-agent {
      margin-top: 2px;
      color: #8a8a8e;
      font-size: 14px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }

    .subagent-status {
      padding: 3px 8px;
      border: 1px solid rgba(142, 142, 147, 0.22);
      color: #5f6065;
      background: rgba(245, 245, 247, 0.78);
      font-size: 13px;
    }

    .subagent-status.completed {
      color: #24663f;
      border-color: rgba(52, 199, 89, 0.22);
      background: rgba(52, 199, 89, 0.10);
    }

    .subagent-status.failed {
      color: #9f2d2f;
      border-color: rgba(255, 59, 48, 0.22);
      background: rgba(255, 59, 48, 0.09);
    }

    .subagent-status.running {
      color: #8a5a00;
      border-color: rgba(255, 204, 0, 0.30);
      background: rgba(255, 204, 0, 0.13);
    }

    .subagent-item-meta {
      margin-top: 7px;
      color: #6e6e73;
      font-size: 14px;
      line-height: 1.4;
    }

    .subagent-item-meta span + span:before {
      content: " · ";
      color: #b0b0b5;
    }

    .subagent-parallel {
      margin-left: 7px;
      padding: 2px 7px;
      border: 1px dashed rgba(65, 108, 157, 0.40);
      color: #416c9d;
      background: #f2f6fb;
      font-size: 12px;
    }

    .subagent-more {
      padding: 4px 14px 14px;
      color: #6e6e73;
      font-size: 14px;
      font-weight: 600;
      text-align: center;
    }

    .subagent-complex {
      display: table;
      width: calc(100% - 28px);
      margin: 12px 14px 14px;
      overflow: hidden;
      border: 1px solid rgba(60, 60, 67, 0.12);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.82);
    }

    .subagent-metric {
      display: table-cell;
      width: 33.333%;
      padding: 11px 8px;
      border-left: 1px solid rgba(60, 60, 67, 0.12);
      text-align: center;
    }

    .subagent-metric:first-child {
      border-left: 0;
    }

    .subagent-metric-value {
      display: block;
      color: #1d1d1f;
      font-size: 18px;
      font-weight: 750;
      line-height: 1.25;
    }

    .subagent-metric-label {
      display: block;
      margin-top: 3px;
      color: #8a8a8e;
      font-size: 12px;
      line-height: 1.3;
    }

    .meta {
      display: table;
      width: 100%;
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid rgba(60, 60, 67, 0.12);
      color: #6e6e73;
      font-size: 15px;
      line-height: 1.45;
    }

    .meta-item {
      display: table-cell;
      width: 50%;
      vertical-align: top;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    .meta-item + .meta-item {
      text-align: right;
      padding-left: 24px;
    }

    .meta-label {
      color: #9a9aa0;
    }
  </style>
</head>
<body>
  {% set title = event.title|default('Webhook 通知', true) %}
  {% set source = event.source|default('AstrBot', true) %}
  {% set status = event.status|default('info', true)|string %}
  {% set status_display = event.status_display|default(status, true)|string %}
  {% set status_text = status|lower %}
  {% set status_tone = 'default' %}
  {% if status_text in ['success', 'ok', 'succeeded', 'completed', '成功', '完成', '已完成'] %}
    {% set status_tone = 'success' %}
  {% elif status_text in ['error', 'failed', 'fail', '错误', '失败', '异常'] %}
    {% set status_tone = 'error' %}
  {% elif status_text in ['warning', 'warn', 'action_required', '警告', '告警'] %}
    {% set status_tone = 'warning' %}
  {% endif %}
  {% set summary = event.summary|default('', true) %}
  {% set generated_time = event.generated_at|default('', true) %}
  {% set event_time = event.event_time|default(event.emitted_at|default('', true), true) %}

  <main class="card">
    <div class="ambient-line"></div>
    <div class="card-inner">
      <div class="topbar">
        <div class="source-wrap">
          <span class="eyebrow">来源：{{ source|e }}</span>
        </div>
        <div class="status-wrap">
          <span class="status-badge status-{{ status_tone }}">{{ status_display|e }}</span>
        </div>
      </div>

      <h1>{{ title|e }}</h1>

      {% if summary|string|trim %}
      <div class="summary">{{ summary|inline_code }}</div>
      {% endif %}

      <div class="section-label">详细信息</div>
      <ul class="fields">
        {% set visible_count = namespace(value=0) %}
        {% if event.fields %}
          {% if event.fields is mapping %}
            {% for field_name, field_value in event.fields.items() %}
              {% set safe_name = field_name|string %}
              {% set safe_key = safe_name|lower %}
              {% set safe_value = field_value if field_value is not none else '' %}
              {% if 'token' not in safe_key and 'raw' not in safe_key and 'prompt' not in safe_key %}
                {% set visible_count.value = visible_count.value + 1 %}
        <li class="field">
          <div class="field-name">{{ safe_name|e }}</div>
          <div class="field-value">{{ safe_value|inline_code }}</div>
        </li>
              {% endif %}
            {% endfor %}
          {% else %}
            {% for field in event.fields %}
              {% set safe_name = field.label|default(field.name|default(field.key|default('字段', true), true), true)|string %}
              {% set safe_value = field.value|default('') %}
              {% if safe_value is none %}
                {% set safe_value = '' %}
              {% endif %}
              {% set safe_key = safe_name|lower %}
              {% if 'token' not in safe_key and 'raw' not in safe_key and 'prompt' not in safe_key %}
                {% set visible_count.value = visible_count.value + 1 %}
        <li class="field">
          <div class="field-name{% if field.secondary %} field-name-secondary{% endif %}">{{ safe_name|e }}</div>
          <div class="field-value{% if field.secondary %} field-value-secondary{% endif %}">{{ safe_value|inline_code }}</div>
        </li>
              {% endif %}
            {% endfor %}
          {% endif %}
        {% endif %}
        {% if visible_count.value == 0 %}
        <li class="empty-fields">暂无可展示字段</li>
        {% endif %}
      </ul>

      {% set timeline = event.subagent_timeline_view|default(none) %}
      {% if timeline %}
      <div class="section-label">子任务执行</div>
      <section class="subagent-panel">
        <div class="subagent-summary">
          <div class="subagent-summary-main">{{ timeline.summary_text|e }}</div>
          {% if timeline.timing_summary %}
          <div class="subagent-summary-meta">{{ timeline.timing_summary|e }}</div>
          {% endif %}
          {% if timeline.flags %}
          <div class="subagent-flags">
            {% for flag in timeline.flags %}<span class="subagent-flag">{{ flag|e }}</span>{% endfor %}
          </div>
          {% endif %}
        </div>

        {% if timeline.notice %}
        <div class="subagent-notice">{{ timeline.notice|e }}</div>
        {% endif %}

        {% if timeline.mode in ['simple', 'degraded'] %}
        <ol class="subagent-list">
          {% for item in timeline.main_items %}
          <li class="subagent-item depth-{{ item.depth_class }}">
            <div class="subagent-item-head">
              <div class="subagent-name-wrap">
                <div class="subagent-name">{{ item.name|e }}{% if item.parallel %}<span class="subagent-parallel">并行</span>{% endif %}</div>
                {% if item.identity %}<div class="subagent-agent">{{ item.identity|e }}</div>{% endif %}
              </div>
              <div class="subagent-state-wrap"><span class="subagent-status {{ item.status_class }}">{{ item.status_symbol|e }} {{ item.status_label|e }}</span></div>
            </div>
            {% if item.meta_labels %}
            <div class="subagent-item-meta">{% for meta in item.meta_labels %}<span>{{ meta|e }}</span>{% endfor %}</div>
            {% endif %}
          </li>
          {% endfor %}
        </ol>
        {% if timeline.main_hidden_count > 0 %}<div class="subagent-more">另有 {{ timeline.main_hidden_count }} 项未展开</div>{% endif %}
        {% else %}
        <div class="subagent-complex">
          {% for metric in timeline.metrics %}
          <div class="subagent-metric"><span class="subagent-metric-value">{{ metric.value|e }}</span><span class="subagent-metric-label">{{ metric.label|e }}</span></div>
          {% endfor %}
        </div>
        {% endif %}
      </section>
      {% endif %}

      <div class="meta">
        <div class="meta-item"><span class="meta-label">生成时间：</span>{{ generated_time|default('未提供', true)|e }}</div>
        <div class="meta-item"><span class="meta-label">事件时间：</span>{{ event_time|default('未提供', true)|e }}</div>
      </div>
    </div>
  </main>
</body>
</html>"""

SUBAGENT_TIMELINE_HTML_TEMPLATE = """\
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --timeline-page: #eef2f6;
      --timeline-shell: #f8fafc;
      --timeline-axis: #e9eff6;
      --timeline-row-odd: #ffffff;
      --timeline-row-even: #f4f7fa;
      --timeline-plot-odd-top: #f0f5fa;
      --timeline-plot-odd-bottom: #eaf1f7;
      --timeline-plot-even-top: #f5f8fb;
      --timeline-plot-even-bottom: #eef3f8;
      --timeline-border: #cbd5e1;
      --timeline-divider: #d5dee8;
      --timeline-text: #243247;
      --timeline-muted: #607086;
      --timeline-axis-text: #43546a;
      --timeline-completed: #6fae88;
      --timeline-failed: #d9878d;
      --timeline-running: #d8ad55;
      --timeline-unknown: #7f9dbe;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      padding: 0;
      width: fit-content;
      min-width: 0;
      min-height: 0;
      color: var(--timeline-text);
      background: var(--timeline-page);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }
    body { padding: 16px; overflow-x: hidden; }
    .card {
      position: relative;
      width: var(--card-width);
      max-width: var(--card-width);
      overflow: hidden;
      border: 1px solid #d7e0e9;
      border-radius: 22px;
      background: #ffffff;
      box-shadow: 0 18px 46px rgba(62,79,101,.11), 0 1px 0 rgba(255,255,255,.9) inset;
    }
    .card:before {
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 1px;
      background: rgba(255,255,255,.9);
    }
    .inner { padding: 27px 32px 23px; }
    .topbar { display: table; width: 100%; margin-bottom: 16px; }
    .eyebrow-wrap, .status-wrap { display: table-cell; vertical-align: top; }
    .status-wrap { text-align: right; }
    .eyebrow, .status-badge, .flag {
      display: inline-block;
      border: 1px solid rgba(0,0,0,.08);
      border-radius: 999px;
      background: rgba(255,255,255,.76);
    }
    .eyebrow { padding: 5px 10px; color: #6e6e73; font-size: 16px; font-weight: 550; }
    .status-badge {
      position: relative;
      padding: 5px 11px 5px 24px;
      color: #5f6065;
      border-color: rgba(142,142,147,.22);
      background: rgba(245,245,247,.78);
      font-size: 16px;
      font-weight: 700;
    }
    .status-badge:before {
      content: "";
      position: absolute;
      left: 10px;
      top: 50%;
      width: 8px;
      height: 8px;
      margin-top: -4px;
      border-radius: 50%;
      background: #8e8e93;
    }
    h1 { margin: 0; font-size: 31px; line-height: 1.18; letter-spacing: -.025em; }
    .subtitle { margin-top: 8px; color: #6e6e73; font-size: 16px; line-height: 1.45; }
    .overview {
      display: table;
      width: 100%;
      margin-top: 16px;
      overflow: hidden;
      border: 1px solid rgba(60,60,67,.12);
      border-radius: 14px;
      background: rgba(245,245,247,.54);
    }
    .metric {
      display: table-cell;
      width: 33.333%;
      padding: 11px 8px;
      border-left: 1px solid rgba(60,60,67,.12);
      text-align: center;
    }
    .metric:first-child { border-left: 0; }
    .metric-value { display: block; font-size: 18px; font-weight: 780; }
    .metric-label { display: block; margin-top: 3px; color: #8a8a8e; font-size: 12px; }
    .flags { margin-top: 10px; }
    .flag { margin: 0 6px 4px 0; padding: 3px 8px; color: #6e6e73; font-size: 12px; font-weight: 700; }
    .section-label { margin: 20px 0 8px; color: #8a8a8e; font-size: 13px; font-weight: 750; letter-spacing: .12em; }
    .timeline {
      overflow: hidden;
      border: 1px solid var(--timeline-border);
      border-radius: 16px;
      background: var(--timeline-shell);
      box-shadow: 0 8px 20px rgba(73,91,115,.08);
    }
    .axis, .row {
      display: grid;
      grid-template-columns: var(--name-column) var(--plot-column) var(--state-column);
    }
    .axis {
      min-height: var(--axis-height);
      color: var(--timeline-axis-text);
      background: var(--timeline-axis);
      font-size: 12px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .axis-name, .axis-state { display: flex; align-items: center; padding: 0 12px; }
    .axis-state { justify-content: flex-end; }
    .axis-track, .track {
      position: relative;
      border-right: 1px solid var(--timeline-divider);
      border-left: 1px solid var(--timeline-divider);
    }
    .axis-track { background: linear-gradient(180deg, #edf3f8, #e4ecf4); }
    .track { overflow: hidden; }
    .tick { position: absolute; bottom: 9px; transform: translateX(-50%); white-space: nowrap; }
    .tick.first { transform: none; }
    .tick.last { transform: translateX(-100%); }
    .gridline {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 1px;
      background: rgba(100,116,139,.13);
    }
    .row {
      min-height: var(--row-min-height);
      border-top: 1px solid rgba(148,163,184,.24);
    }
    .timeline-row:nth-child(odd) .task,
    .timeline-row:nth-child(odd) .state { background: var(--timeline-row-odd); }
    .timeline-row:nth-child(odd) .track { background: linear-gradient(180deg, var(--timeline-plot-odd-top), var(--timeline-plot-odd-bottom)); }
    .timeline-row:nth-child(even) .task,
    .timeline-row:nth-child(even) .state { background: var(--timeline-row-even); }
    .timeline-row:nth-child(even) .track { background: linear-gradient(180deg, var(--timeline-plot-even-top), var(--timeline-plot-even-bottom)); }
    .task {
      min-width: 0;
      padding: var(--task-padding-y) 12px;
      border-left: 3px solid transparent;
      color: var(--timeline-text);
    }
    .task.depth-2 { padding-left: 21px; border-left-color: rgba(84,116,153,.28); }
    .task.depth-3 { padding-left: 31px; border-left-color: rgba(84,116,153,.35); }
    .task.depth-4 { padding-left: 41px; border-left-color: rgba(84,116,153,.42); }
    .task-name {
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: normal;
      hyphens: auto;
    }
    .task-name { font-size: var(--task-font-size); font-weight: 730; line-height: var(--task-line-height); }
    .task-agent {
      margin-top: 3px;
      color: var(--timeline-muted);
      font-size: var(--agent-font-size);
      line-height: 1.25;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: normal;
    }
    .density-dense .task-agent { display: none; }
    .bar {
      position: absolute;
      top: 50%;
      height: var(--bar-height);
      min-width: 0;
      transform: translateY(-50%);
      border: 1px solid #5f7f9f;
      border-radius: 7px;
      background: var(--timeline-unknown);
      box-shadow: 0 2px 6px rgba(66,83,105,.16);
    }
    .bar.completed { border-color: #4d8c69; background: var(--timeline-completed); }
    .bar.failed { border-color: #b85b63; background: var(--timeline-failed); }
    .bar.running { border-color: #b1842e; background: var(--timeline-running); }
    .bar.cancelled { border-color: #7b8795; background: #a8b3c0; }
    .bar.unknown { border-color: #5f7f9f; background: var(--timeline-unknown); }
    .bar.partial {
      border-style: dashed;
      background: repeating-linear-gradient(135deg, #b8c6d6 0, #b8c6d6 6px, #dce4ed 6px, #dce4ed 11px);
    }
    .state {
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: flex-end;
      min-width: 0;
      padding: var(--task-padding-y) 11px;
      text-align: right;
    }
    .state-label { color: #53657a; font-size: 12px; font-weight: 750; white-space: nowrap; }
    .state-label.completed { color: #2f7550; }
    .state-label.failed { color: #a43e48; }
    .state-label.running { color: #8a6218; }
    .state-label.unknown { color: #4b6d90; }
    .state-label.cancelled { color: #5f6b7a; }
    .state-time { margin-top: 3px; color: var(--timeline-muted); font-size: 11px; white-space: nowrap; }
    .unlocated {
      margin-top: 12px;
      padding: 12px;
      border: 1px dashed #a8b6c7;
      border-radius: 13px;
      background: var(--timeline-row-even);
    }
    .unlocated-title { color: var(--timeline-text); font-size: 14px; font-weight: 750; }
    .unlocated-list { margin: 8px 0 0; padding: 0; list-style: none; }
    .unlocated-item {
      display: grid;
      grid-template-columns: minmax(0,1fr) auto;
      gap: 14px;
      width: 100%;
      min-height: var(--row-min-height);
      padding: var(--task-padding-y) 0;
      border-top: 1px solid rgba(148,163,184,.24);
    }
    .unlocated-item:first-child { border-top: 0; }
    .unlocated-name, .unlocated-state { align-self: center; font-size: 13px; }
    .unlocated-name { color: var(--timeline-text); font-weight: 680; white-space: normal; overflow-wrap: anywhere; word-break: normal; }
    .unlocated-state { color: var(--timeline-muted); white-space: nowrap; text-align: right; }
    .legend {
      margin-top: 12px;
      color: var(--timeline-muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .legend-item { display: inline-block; margin: 0 14px 5px 0; white-space: nowrap; }
    .legend-mark { display: inline-block; width: 17px; height: 8px; margin-right: 5px; border-radius: 3px; vertical-align: 1px; }
    .legend-mark.completed { background: var(--timeline-completed); border: 1px solid #4d8c69; }
    .legend-mark.failed { background: var(--timeline-failed); border: 1px solid #b85b63; }
    .legend-mark.running { background: var(--timeline-running); border: 1px solid #b1842e; }
    .legend-mark.cancelled { background: #a8b3c0; border: 1px solid #7b8795; }
    .legend-mark.unknown { background: var(--timeline-unknown); border: 1px solid #5f7f9f; }
    .legend-mark.partial { background: #cbd6e2; border: 1px dashed #7e8fa3; }
  </style>
</head>
<body>
  <main class="card density-{{ event.layout.density }}" style="--card-width: {{ event.layout.card_width }}px; --name-column: {{ event.layout.name_column_width }}px; --plot-column: {{ event.layout.plot_width }}px; --state-column: {{ event.layout.state_column_width }}px; --row-min-height: {{ event.layout.row_min_height }}px; --task-font-size: {{ event.layout.task_font_size }}px; --task-line-height: {{ event.layout.task_line_height }}; --task-padding-y: {{ event.layout.task_padding_y }}px; --bar-height: {{ event.layout.bar_height }}px; --axis-height: {{ event.layout.axis_height }}px; --agent-font-size: {% if event.layout.density == 'comfortable' %}11{% else %}10{% endif %}px;">
    <div class="inner">
      <div class="topbar">
        <div class="eyebrow-wrap"><span class="eyebrow">根任务 · 相对时间</span></div>
        <div class="status-wrap"><span class="status-badge">相对时间线</span></div>
      </div>
      <h1>子任务执行时间线</h1>
      <div class="subtitle">{{ event.subtitle|e }}</div>
      <div class="overview">
        {% for metric in event.metrics %}<div class="metric"><span class="metric-value">{{ metric.value|e }}</span><span class="metric-label">{{ metric.label|e }}</span></div>{% endfor %}
      </div>
      {% if event.flags %}<div class="flags">{% for flag in event.flags %}<span class="flag">{{ flag|e }}</span>{% endfor %}</div>{% endif %}

      <div class="section-label">执行区间</div>
      <section class="timeline">
        <div class="axis">
          <div class="axis-name">子任务</div>
          <div class="axis-track">{% for tick in event.axis_ticks %}<span class="tick {{ tick.edge_class }}" style="left: {{ tick.left_px }}px;">{{ tick.label|e }}</span>{% endfor %}</div>
          <div class="axis-state">状态 / 耗时</div>
        </div>
        {% for item in event.gantt_items %}
        <div class="row timeline-row" style="min-height: {{ item.estimated_row_height }}px;">
          <div class="task depth-{{ item.depth_class }}"><div class="task-name">{{ item.name|e }}</div>{% if item.identity %}<div class="task-agent">{{ item.identity|e }}</div>{% endif %}</div>
          <div class="track">{% for line in event.grid_lines %}<span class="gridline" style="left: {{ line.left_px }}px;"></span>{% endfor %}<span class="bar {{ item.status_class }}{% if item.partial_interval %} partial{% endif %}{% if item.minimum_width_applied %} minimum-width{% endif %}" style="left: {{ item.left_px }}px; width: {{ item.width_px }}px;"></span></div>
          <div class="state"><span class="state-label {{ item.status_class }}">{{ item.status_symbol|e }} {{ item.status_label|e }}</span><span class="state-time">{{ item.timing_label|e }}</span></div>
        </div>
        {% endfor %}
      </section>

      {% if event.unlocated_items %}
      <section class="unlocated">
        <div class="unlocated-title">未定位任务 · 缺少完整起止时间</div>
        <ul class="unlocated-list">
          {% for item in event.unlocated_items %}<li class="unlocated-item" style="min-height: {{ item.estimated_row_height }}px;"><span class="unlocated-name">{{ item.name|e }}{% if item.identity %} · {{ item.identity|e }}{% endif %}</span><span class="unlocated-state">{{ item.status_symbol|e }} {{ item.status_label|e }} · 区间不完整</span></li>{% endfor %}
        </ul>
      </section>
      {% endif %}

      <div class="legend">
        <span class="legend-item"><span class="legend-mark completed"></span>已完成</span>
        <span class="legend-item"><span class="legend-mark failed"></span>失败</span>
        <span class="legend-item"><span class="legend-mark running"></span>进行中</span>
        <span class="legend-item"><span class="legend-mark cancelled"></span>已取消</span>
        <span class="legend-item"><span class="legend-mark unknown"></span>状态未知</span>
        <span class="legend-item"><span class="legend-mark partial"></span>区间不完整</span>
      </div>
    </div>
  </main>
</body>
</html>"""


def _render_inline_code(value: Any) -> Markup:
    """安全渲染成对单反引号，不解释其他 Markdown 或 HTML。"""

    text = "" if value is None else str(value)
    parts: list[Markup] = []
    cursor = 0
    for match in _INLINE_CODE_RE.finditer(text):
        parts.append(Markup.escape(text[cursor : match.start()]))
        parts.append(
            Markup("<code>") + Markup.escape(match.group(1)) + Markup("</code>")
        )
        cursor = match.end()
    parts.append(Markup.escape(text[cursor:]))
    return Markup("").join(parts)


def _is_finite_timeline_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _safe_timeline_display_text(value: Any, *, fallback: str = "") -> str:
    """Return bounded display text without surfacing refs, paths, or raw JSON."""

    if not isinstance(value, str):
        return fallback
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return fallback
    lowered = text.lower()
    path_like = (
        text.startswith(("/", "~/", "file:"))
        or _TIMELINE_WINDOWS_PATH_RE.match(text) is not None
        or re.search(r"/(?:users|home|var|tmp|volumes|private)/", lowered) is not None
    )
    json_like = (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )
    if path_like or json_like or _TIMELINE_REF_LIKE_RE.fullmatch(text):
        return fallback
    return text


def _timeline_identity_text(
    agent: Any,
    model: Any,
    model_variant: Any,
) -> str:
    """Compose the bounded public identity for one timeline item."""

    agent_text = _safe_timeline_display_text(agent)
    model_text = _safe_timeline_display_text(model)
    variant_text = _safe_timeline_display_text(model_variant)
    if model_text and variant_text.lower() != "default":
        model_text = f"{model_text}({variant_text})" if variant_text else model_text
    return " · ".join(value for value in (agent_text, model_text) if value)


def _timeline_identity_from_item(item: dict[str, Any]) -> str:
    return _timeline_identity_text(
        item.get("agent"),
        item.get("model"),
        item.get("modelVariant"),
    )


def _is_auxiliary_timeline_item(item: dict[str, Any]) -> bool:
    for key in ("name", "agent"):
        value = item.get(key)
        if isinstance(value, str) and value.strip().lower() == "smartfetch-secondary":
            return True
    return False


def _timeline_interval(item: dict[str, Any]) -> tuple[float, float] | None:
    start = item.get("startOffsetMs")
    end = item.get("endOffsetMs")
    if (
        isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(start)
        or start < 0
        or isinstance(end, bool)
        or not isinstance(end, (int, float))
        or not math.isfinite(end)
        or end < 0
    ):
        return None
    start_value = float(start)
    end_value = float(end)
    if end_value < start_value:
        return None
    return start_value, end_value


def _timeline_reliable_interval(item: dict[str, Any]) -> tuple[float, float] | None:
    interval = _timeline_interval(item)
    if interval is None or item.get("timingQuality") not in {"observed", "fallback"}:
        return None
    return interval


def _timeline_peak_concurrency(intervals: list[tuple[float, float]]) -> int:
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        if end <= start:
            continue
        events.append((start, 1))
        events.append((end, -1))
    current = 0
    peak = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        current += delta
        peak = max(peak, current)
    if intervals and peak == 0:
        return 1
    return peak


def _timeline_overlap_data(
    intervals: list[tuple[int, float, float]],
) -> tuple[set[int], int]:
    parallel_indices: set[int] = set()
    overlap_pairs = 0
    for left_index, (item_index, start, end) in enumerate(intervals):
        if end <= start:
            continue
        for other_index, other_start, other_end in intervals[left_index + 1 :]:
            if other_end <= other_start:
                continue
            if max(start, other_start) < min(end, other_end):
                parallel_indices.update((item_index, other_index))
                overlap_pairs += 1
    return parallel_indices, overlap_pairs


def _format_timeline_scale(value_ms: float) -> str:
    value = max(0.0, value_ms)
    if value < 1:
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{text}毫秒"
    if value < 1000:
        return f"{int(round(value))}毫秒"
    seconds = value / 1000
    if seconds < 60:
        text = f"{seconds:.1f}".rstrip("0").rstrip(".")
        return f"{text}秒"
    total_seconds = int(round(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return (
            f"{minutes}分{remaining_seconds}秒" if remaining_seconds else f"{minutes}分"
        )
    if minutes >= 24 * 60:
        days, remaining_day_minutes = divmod(minutes, 24 * 60)
        remaining_hours, remaining_minutes = divmod(remaining_day_minutes, 60)
        if remaining_hours or remaining_minutes:
            suffix = f"{remaining_hours}小时" if remaining_hours else ""
            if remaining_minutes:
                suffix += f"{remaining_minutes}分"
            return f"{days}天{suffix}"
        return f"{days}天"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}小时{remaining_minutes}分" if remaining_minutes else f"{hours}小时"


def _timeline_visual_units(value: str) -> float:
    """Approximate rendered width while keeping layout independent from a browser pass."""

    units = 0.0
    for char in value:
        if char.isspace():
            units += 0.35
        elif unicodedata.east_asian_width(char) in {"W", "F", "A"}:
            units += 1.0
        else:
            units += 0.55
    return max(1.0, units)


def _timeline_percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 1.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def _timeline_density(item_count: int) -> str:
    if item_count <= 24:
        return "comfortable"
    if item_count <= 48:
        return "compact"
    return "dense"


def _timeline_estimated_lines(
    text: str,
    *,
    column_width: int,
    font_size: int,
    depth_class: int = 1,
) -> int:
    depth_padding = 22 + max(0, min(depth_class, 4) - 1) * 10
    available_width = max(120, column_width - depth_padding)
    estimated_width = _timeline_visual_units(text) * font_size
    return max(1, math.ceil(estimated_width / available_width))


def _build_subagent_timeline_layout(
    item_views: list[dict[str, Any]],
    *,
    timeline_end_ms: float,
    max_depth: int,
) -> SubagentTimelineLayout:
    item_count = len(item_views)
    density = _timeline_density(item_count)
    density_view = _TIMELINE_DENSITY_VIEW[density]
    name_units = [_timeline_visual_units(str(item["name"])) for item in item_views]
    p90_name_units = _timeline_percentile(name_units, 0.90)
    max_name_units = max(name_units, default=1.0)
    depth_indent = max(0, min(max_depth, 4) - 1) * 10
    name_width_target = max(
        56 + 7.2 * p90_name_units + depth_indent,
        56 + 4.8 * max_name_units + depth_indent,
    )
    name_column_width = int(
        min(
            SUBAGENT_TIMELINE_MAX_NAME_COLUMN_WIDTH,
            max(
                SUBAGENT_TIMELINE_MIN_NAME_COLUMN_WIDTH,
                math.ceil(name_width_target),
            ),
        )
    )

    span_minutes = timeline_end_ms / 60_000
    plot_target = int(
        min(
            SUBAGENT_TIMELINE_MAX_PLOT_WIDTH,
            max(
                SUBAGENT_TIMELINE_MIN_PLOT_WIDTH,
                880 + 8 * min(span_minutes, 90) + 10 * max(item_count - 8, 0),
            ),
        )
    )
    horizontal_chrome = (
        SUBAGENT_TIMELINE_BODY_PADDING * 2
        + SUBAGENT_TIMELINE_INNER_PADDING * 2
        + SUBAGENT_TIMELINE_STATE_COLUMN_WIDTH
    )
    desired_viewport = name_column_width + plot_target + horizontal_chrome
    viewport_width = min(
        SUBAGENT_TIMELINE_MAX_VIEWPORT_WIDTH,
        max(
            SUBAGENT_TIMELINE_MIN_VIEWPORT_WIDTH,
            int(math.ceil(desired_viewport / 32) * 32),
        ),
    )
    card_width = viewport_width - SUBAGENT_TIMELINE_BODY_PADDING * 2
    plot_width = (
        card_width
        - SUBAGENT_TIMELINE_INNER_PADDING * 2
        - SUBAGENT_TIMELINE_BORDER_WIDTH * 4
        - name_column_width
        - SUBAGENT_TIMELINE_STATE_COLUMN_WIDTH
    )

    font_size = int(density_view["task_font_size"])
    line_height = float(density_view["task_line_height"])
    padding_y = int(density_view["task_padding_y"])
    row_min_height = int(density_view["row_min_height"])
    agent_font_size = int(density_view["agent_font_size"])
    show_agent = bool(density_view["show_agent"])
    located_height = 0
    unlocated_height = 0
    for item in item_views:
        name_lines = _timeline_estimated_lines(
            str(item["name"]),
            column_width=name_column_width,
            font_size=font_size,
            depth_class=int(item["depth_class"]),
        )
        identity = str(item.get("identity") or "")
        identity_lines = (
            _timeline_estimated_lines(
                identity,
                column_width=name_column_width,
                font_size=agent_font_size,
                depth_class=int(item["depth_class"]),
            )
            if show_agent and identity
            else 0
        )
        agent_height = (
            math.ceil(identity_lines * agent_font_size * 1.25) + 3
            if identity_lines
            else 0
        )
        estimated_row_height = max(
            row_min_height,
            math.ceil(name_lines * font_size * line_height)
            + padding_y * 2
            + agent_height,
        )
        item["estimated_name_lines"] = name_lines
        item["estimated_row_height"] = estimated_row_height
        if item["located"]:
            located_height += estimated_row_height
        else:
            unlocated_height += estimated_row_height

    vertical_chrome_height = 470 + int(density_view["axis_height"])
    estimated_height = vertical_chrome_height + located_height
    if unlocated_height:
        estimated_height += 60 + unlocated_height
    soft_height_exceeded = estimated_height > SUBAGENT_TIMELINE_SOFT_HEIGHT
    viewport_height = min(
        SUBAGENT_TIMELINE_MAX_VIEWPORT_HEIGHT,
        max(SUBAGENT_TIMELINE_MIN_VIEWPORT_HEIGHT, estimated_height),
    )
    overflow_timeout = (
        math.ceil((estimated_height - SUBAGENT_TIMELINE_SOFT_HEIGHT) / 750) * 1000
        if soft_height_exceeded
        else 0
    )
    render_timeout_ms = min(
        SUBAGENT_TIMELINE_MAX_TIMEOUT_MS,
        int(density_view["base_timeout_ms"]) + overflow_timeout,
    )
    tick_target_count = max(5, min(10, plot_width // 180 + 1))

    return SubagentTimelineLayout(
        viewport_width=viewport_width,
        card_width=card_width,
        name_column_width=name_column_width,
        plot_width=plot_width,
        state_column_width=SUBAGENT_TIMELINE_STATE_COLUMN_WIDTH,
        density=density,
        row_min_height=row_min_height,
        task_font_size=font_size,
        task_line_height=line_height,
        task_padding_y=padding_y,
        bar_height=int(density_view["bar_height"]),
        axis_height=int(density_view["axis_height"]),
        tick_target_count=tick_target_count,
        located_rows_height=located_height,
        unlocated_rows_height=unlocated_height,
        vertical_chrome_height=vertical_chrome_height,
        estimated_height=estimated_height,
        viewport_height=viewport_height,
        soft_height_exceeded=soft_height_exceeded,
        render_timeout_ms=render_timeout_ms,
        prefer_normal_scale=viewport_width > 1920 or soft_height_exceeded,
    )


_TIMELINE_NICE_STEP_FACTORS = (1.0, 2.0, 2.5, 5.0)
_TIMELINE_TICK_SAFETY_GAP_PX = 12.0


def _timeline_decimal_places(value: float, *, maximum: int = 6) -> int:
    if not math.isfinite(value) or value <= 0:
        return 0
    tolerance = max(1e-12, abs(value) * 1e-12)
    for decimal_places in range(maximum + 1):
        if math.isclose(
            value,
            round(value, decimal_places),
            rel_tol=0,
            abs_tol=tolerance,
        ):
            return decimal_places
    return maximum


def _format_timeline_decimal(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _format_timeline_tick(value_ms: float, step_ms: float) -> str:
    value = max(0.0, value_ms)
    if value == 0:
        return "0"
    if value < 1000:
        decimals = max(
            _timeline_decimal_places(step_ms),
            _timeline_decimal_places(value),
        )
        text = _format_timeline_decimal(value, decimals)
        return f"+{text}毫秒"
    if value < 60_000:
        step_seconds = step_ms / 1000
        value_seconds = value / 1000
        decimals = max(
            _timeline_decimal_places(step_seconds),
            _timeline_decimal_places(value_seconds),
        )
        text = _format_timeline_decimal(value_seconds, decimals)
        return f"+{text}秒"
    return f"+{_format_timeline_scale(value)}"


def _timeline_tick_label_width(label: str) -> float:
    return max(16.0, _timeline_visual_units(label) * 11.5 + 8.0)


def _timeline_tick_geometry(
    values: list[float],
    *,
    timeline_end_ms: float,
    plot_width: int,
    step_ms: float,
) -> tuple[list[str], list[float], int, float, float]:
    labels = [_format_timeline_tick(value, step_ms) for value in values]
    positions = [value / timeline_end_ms * plot_width for value in values]
    overlap_count = 0
    minimum_slack = math.inf
    gaps: list[float] = []
    for index in range(len(values) - 1):
        gap = positions[index + 1] - positions[index]
        required = (
            _timeline_tick_label_width(labels[index])
            + _timeline_tick_label_width(labels[index + 1])
        ) / 2 + _TIMELINE_TICK_SAFETY_GAP_PX
        slack = gap - required
        minimum_slack = min(minimum_slack, slack)
        if slack < 0:
            overlap_count += 1
        gaps.append(gap)
    imbalance = max(gaps) / min(gaps) if gaps and min(gaps) > 0 else 1.0
    tail_px = plot_width - positions[-1]
    return (
        labels,
        positions,
        overlap_count,
        minimum_slack,
        max(1.0, imbalance) + tail_px / plot_width,
    )


def _timeline_tick_values_for_step(
    timeline_end_ms: float,
    *,
    step_ms: float,
    plot_width: int,
) -> list[float]:
    interval_count = int(math.floor(timeline_end_ms / step_ms))
    if interval_count > 32:
        return []
    values = [index * step_ms for index in range(interval_count + 1)]
    last = values[-1]
    if math.isclose(last, timeline_end_ms, rel_tol=1e-12, abs_tol=1e-9):
        values[-1] = timeline_end_ms
        return values

    appended = [*values, timeline_end_ms]
    appended_labels, _, appended_overlaps, _, _ = _timeline_tick_geometry(
        appended,
        timeline_end_ms=timeline_end_ms,
        plot_width=plot_width,
        step_ms=step_ms,
    )
    appended_is_valid = appended_overlaps == 0 and len(appended_labels) == len(
        set(appended_labels)
    )
    remainder = timeline_end_ms - last
    if appended_is_valid and remainder >= step_ms / 1.75:
        return appended

    if len(values) > 1:
        replaced = [*values[:-1], timeline_end_ms]
        replaced_labels, _, replaced_overlaps, _, _ = _timeline_tick_geometry(
            replaced,
            timeline_end_ms=timeline_end_ms,
            plot_width=plot_width,
            step_ms=step_ms,
        )
        if replaced_overlaps == 0 and len(replaced_labels) == len(set(replaced_labels)):
            return replaced
    if appended_is_valid:
        return appended
    return values


def _timeline_nice_tick_values(
    timeline_end_ms: float, layout: SubagentTimelineLayout
) -> tuple[list[float], float]:
    target_count = layout.tick_target_count
    raw_step = timeline_end_ms / max(1, target_count - 1)
    if raw_step >= 86_400_000:
        unit_ms = 86_400_000.0
    elif raw_step >= 3_600_000:
        unit_ms = 3_600_000.0
    elif raw_step >= 60_000:
        unit_ms = 60_000.0
    elif raw_step >= 1000:
        unit_ms = 1000.0
    else:
        unit_ms = 1.0
    scaled_raw_step = raw_step / unit_ms
    exponent = math.floor(math.log10(scaled_raw_step)) if raw_step > 0 else 0
    candidates: list[tuple[tuple[float, ...], list[float], float]] = []
    for power in range(exponent - 2, exponent + 3):
        magnitude = 10.0**power * unit_ms
        for factor in _TIMELINE_NICE_STEP_FACTORS:
            step = factor * magnitude
            values = _timeline_tick_values_for_step(
                timeline_end_ms,
                step_ms=step,
                plot_width=layout.plot_width,
            )
            if not values:
                continue
            count = len(values)
            labels, positions, overlap_count, minimum_slack, balance_penalty = (
                _timeline_tick_geometry(
                    values,
                    timeline_end_ms=timeline_end_ms,
                    plot_width=layout.plot_width,
                    step_ms=step,
                )
            )
            duplicate_count = len(labels) - len(set(labels))
            non_monotonic_count = sum(
                right <= left for left, right in zip(positions, positions[1:])
            )
            range_penalty = float(max(0, 5 - count) + max(0, count - 10))
            target_penalty = abs(count - target_count)
            step_penalty = abs(math.log10(step / raw_step)) if raw_step > 0 else 0.0
            candidates.append(
                (
                    (
                        float(overlap_count + duplicate_count + non_monotonic_count),
                        range_penalty,
                        max(0.0, -minimum_slack),
                        balance_penalty,
                        float(target_penalty),
                        step_penalty,
                    ),
                    values,
                    step,
                )
            )
    if not candidates:
        return [0.0, timeline_end_ms], timeline_end_ms
    selected = min(candidates, key=lambda candidate: candidate[0])
    return selected[1], selected[2]


def _build_timeline_ticks(
    timeline_end_ms: float, layout: SubagentTimelineLayout
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values, step_ms = _timeline_nice_tick_values(timeline_end_ms, layout)

    ticks: list[dict[str, Any]] = []
    grid_lines: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        left_px = min(
            layout.plot_width, max(0.0, value / timeline_end_ms * layout.plot_width)
        )
        edge_class = (
            "first" if index == 0 else "last" if index == len(values) - 1 else ""
        )
        tick = {
            "value_ms": value,
            "left_px": f"{left_px:.2f}".rstrip("0").rstrip("."),
            "edge_class": edge_class,
            "label": _format_timeline_tick(value, step_ms),
            "label_width_px": _timeline_tick_label_width(
                _format_timeline_tick(value, step_ms)
            ),
        }
        ticks.append(tick)
        if index not in {0, len(values) - 1}:
            grid_lines.append({"left_px": tick["left_px"]})
    return ticks, grid_lines


def _timeline_status_view(status: Any) -> tuple[str, str, str]:
    status_key = (
        status
        if isinstance(status, str) and status in _TIMELINE_STATUSES
        else "unknown"
    )
    label, symbol = _TIMELINE_STATUS_VIEW[status_key]
    return status_key, label, symbol


def _timeline_is_root_completion(event: NormalizedEvent) -> bool:
    scope = getattr(event.session_scope, "value", event.session_scope)
    return event.event == "opencode.session_idle" and scope == "root"


def _build_subagent_timeline_view(event: NormalizedEvent) -> dict[str, Any] | None:
    """Build a deterministic, display-only timeline view without raw identifiers."""

    timeline = event.subagent_timeline
    if not _timeline_is_root_completion(event) or not isinstance(timeline, dict):
        return None
    raw_items = timeline.get("items")
    if not isinstance(raw_items, list):
        return None
    if len(raw_items) > SUBAGENT_TIMELINE_MAX_ITEMS:
        raise ValueError("subagent timeline items exceed renderer wire limit")
    indexed_items = [
        (index, item)
        for index, item in enumerate(raw_items)
        if isinstance(item, dict) and not _is_auxiliary_timeline_item(item)
    ]
    if not indexed_items:
        return None

    item_count = len(indexed_items)
    excluded_count = len(raw_items) - item_count
    observed_raw = timeline.get("observedItemCount")
    observed_count = (
        max(item_count, observed_raw - excluded_count)
        if isinstance(observed_raw, int) and not isinstance(observed_raw, bool)
        else item_count
    )
    truncated = timeline.get("truncated") is True or observed_count > item_count
    partial = timeline.get("partial") is True

    status_counts = {status: 0 for status in _TIMELINE_STATUSES}
    all_intervals: list[tuple[int, float, float]] = []
    reliable_intervals: list[tuple[float, float]] = []
    missing_count = 0
    max_depth = 1
    for index, item in indexed_items:
        status_key, _, _ = _timeline_status_view(item.get("status"))
        status_counts[status_key] += 1
        depth = item.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool):
            max_depth = max(max_depth, depth)
        interval = _timeline_interval(item)
        if interval is None:
            missing_count += 1
        else:
            all_intervals.append((index, interval[0], interval[1]))
        reliable_interval = _timeline_reliable_interval(item)
        if reliable_interval is not None:
            reliable_intervals.append(reliable_interval)

    missing_ratio = missing_count / item_count
    parallel_indices, overlap_pairs = _timeline_overlap_data(all_intervals)
    observed_peak = _timeline_peak_concurrency(
        [(start, end) for _, start, end in all_intervals]
    )
    reliable_peak = _timeline_peak_concurrency(reliable_intervals)
    reliable_span_ms = None
    if reliable_intervals:
        reliable_span_ms = max(end for _, end in reliable_intervals) - min(
            start for start, _ in reliable_intervals
        )

    complexity_score = max(0, item_count - SUBAGENT_TIMELINE_SIMPLE_BASE_ITEMS)
    complexity_score += 2 * max(0, max_depth - SUBAGENT_TIMELINE_SIMPLE_MAX_DEPTH)
    complexity_score += 2 * max(
        0, observed_peak - SUBAGENT_TIMELINE_SIMPLE_MAX_CONCURRENCY
    )
    if overlap_pairs >= 2:
        complexity_score += 1
    if partial:
        complexity_score += 1
    if truncated:
        complexity_score += 2

    if missing_ratio > SUBAGENT_TIMELINE_MISSING_RATIO_LIMIT:
        mode = "degraded"
    else:
        hard_complex = (
            max_depth > SUBAGENT_TIMELINE_SIMPLE_MAX_DEPTH
            or observed_peak > SUBAGENT_TIMELINE_SIMPLE_MAX_CONCURRENCY
            or truncated
        )
        mode = (
            "complex"
            if hard_complex
            or complexity_score >= SUBAGENT_TIMELINE_COMPLEXITY_THRESHOLD
            else "simple"
        )

    timeline_end_ms = max((end for _, _, end in all_intervals), default=1.0)
    timeline_end_ms = max(1.0, timeline_end_ms)
    item_views: list[dict[str, Any]] = []
    for index, item in indexed_items:
        status_class, status_label, status_symbol = _timeline_status_view(
            item.get("status")
        )
        name = _safe_timeline_display_text(item.get("name"), fallback="未命名子任务")
        agent = _safe_timeline_display_text(item.get("agent"))
        model = _safe_timeline_display_text(item.get("model"))
        model_variant = _safe_timeline_display_text(item.get("modelVariant"))
        identity = _timeline_identity_text(agent, model, model_variant)
        depth = item.get("depth")
        depth_value = (
            depth if isinstance(depth, int) and not isinstance(depth, bool) else 1
        )
        depth_class = min(4, max(1, depth_value))
        interval = _timeline_interval(item)
        reliable_interval = _timeline_reliable_interval(item)
        duration = item.get("durationMs")
        duration_label = None
        if (
            reliable_interval is not None
            and isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and duration >= 0
            and item.get("timingQuality") != "partial"
        ):
            duration_label = format_duration_ms(duration)

        meta_labels: list[str] = []
        if reliable_interval is not None:
            meta_labels.append(f"+{_format_timeline_scale(reliable_interval[0])}")
            if duration_label:
                meta_labels.append(duration_label)
        elif interval is not None or item.get("timingQuality") == "partial":
            meta_labels.append("区间不完整")
        else:
            meta_labels.append("时间未定位")

        timing_label = (
            duration_label
            if duration_label
            else "区间不完整"
            if interval is None or item.get("timingQuality") == "partial"
            else "已定位"
        )
        item_views.append(
            {
                "source_index": index,
                "name": name,
                "agent": agent,
                "model": model,
                "model_variant": model_variant,
                "identity": identity,
                "status_class": status_class,
                "status_label": status_label,
                "status_symbol": status_symbol,
                "depth_class": depth_class,
                "parallel": index in parallel_indices,
                "meta_labels": meta_labels,
                "located": interval is not None,
                "partial_interval": interval is not None
                and item.get("timingQuality") == "partial",
                "interval_start": interval[0] if interval is not None else None,
                "interval_end": interval[1] if interval is not None else None,
                "timing_label": timing_label,
                "sort_start": interval[0] if interval is not None else math.inf,
            }
        )

    item_views.sort(key=lambda item: (item["sort_start"], item["source_index"]))
    layout = _build_subagent_timeline_layout(
        item_views,
        timeline_end_ms=timeline_end_ms,
        max_depth=max_depth,
    )
    px_per_ms = layout.plot_width / timeline_end_ms
    for item in item_views:
        interval_start = item.pop("interval_start", None)
        interval_end = item.pop("interval_end", None)
        if isinstance(interval_start, (int, float)) and isinstance(
            interval_end, (int, float)
        ):
            actual_left_px = min(
                float(layout.plot_width), max(0.0, interval_start * px_per_ms)
            )
            duration_ms = max(0.0, interval_end - interval_start)
            actual_width_px = duration_ms * px_per_ms
            visual_width_px = (
                max(float(SUBAGENT_TIMELINE_MIN_BAR_WIDTH), actual_width_px)
                if duration_ms > 0
                else 2.0
            )
            visual_width_px = min(float(layout.plot_width), visual_width_px)
            visual_left_px = min(
                actual_left_px, max(0.0, layout.plot_width - visual_width_px)
            )
            item["left_px"] = f"{visual_left_px:.2f}".rstrip("0").rstrip(".")
            item["width_px"] = f"{visual_width_px:.2f}".rstrip("0").rstrip(".")
            item["minimum_width_applied"] = (
                duration_ms > 0 and actual_width_px < SUBAGENT_TIMELINE_MIN_BAR_WIDTH
            )
        else:
            item["left_px"] = "0"
            item["width_px"] = "0"
            item["minimum_width_applied"] = False
        item.pop("sort_start", None)
        item.pop("source_index", None)

    status_parts = [
        f"{_TIMELINE_STATUS_VIEW[status][0]} {count}"
        for status, count in status_counts.items()
        if count
    ]
    summary_text = f"{observed_count} 个子任务"
    if status_parts:
        summary_text += " · " + " · ".join(status_parts)

    observation_limited = partial or truncated
    peak_metric_label = "已观测峰值并发" if observation_limited else "峰值并发"
    span_metric_label = "已观测跨度" if observation_limited else "总跨度"
    timing_parts: list[str] = []
    if reliable_peak:
        timing_parts.append(f"{peak_metric_label} {reliable_peak}")
    if reliable_span_ms is not None:
        timing_parts.append(
            f"{span_metric_label} {format_duration_ms(reliable_span_ms)}"
        )
    timing_summary = " · ".join(timing_parts)

    flags: list[str] = []
    if partial:
        flags.append("部分数据")
    if truncated:
        flags.append("记录已截断")
    if observed_count > item_count:
        flags.append(f"已记录 {item_count}/{observed_count}")
    if missing_count:
        flags.append(f"{missing_count} 项时间不完整")
    if max_depth > 1:
        flags.append("包含嵌套执行")

    if mode == "degraded":
        notice = "部分时间数据缺失，以下仅按已观测信息展示。"
    elif mode == "complex":
        notice = "流程较复杂，主卡仅展示关键摘要。"
    elif partial:
        notice = "部分执行区间不完整，未显示精确耗时。"
    else:
        notice = ""

    main_items = item_views[:SUBAGENT_TIMELINE_MAIN_ITEM_LIMIT]
    main_hidden_count = max(0, observed_count - len(main_items))
    reliable_span_label = (
        format_duration_ms(reliable_span_ms)
        if isinstance(reliable_span_ms, (int, float))
        else "不可用"
    )
    metrics = [
        {"value": str(observed_count), "label": "子任务"},
        {
            "value": str(reliable_peak) if reliable_peak else "—",
            "label": peak_metric_label,
        },
        {
            "value": reliable_span_label,
            "label": span_metric_label,
        },
    ]

    located_items = [item for item in item_views if item["located"]]
    unlocated_items = [item for item in item_views if not item["located"]]
    gantt_displayed_count = len(item_views)
    observation_subtitle = " · 指标按已观测范围计算" if observation_limited else ""
    axis_ticks, grid_lines = _build_timeline_ticks(timeline_end_ms, layout)

    return {
        "mode": mode,
        "complexity_score": complexity_score,
        "item_count": item_count,
        "observed_count": observed_count,
        "max_depth": max_depth,
        "peak_concurrency": observed_peak,
        "missing_count": missing_count,
        "missing_ratio": missing_ratio,
        "partial": partial,
        "truncated": truncated,
        "summary_text": summary_text,
        "timing_summary": timing_summary,
        "flags": flags,
        "notice": notice,
        "main_items": main_items,
        "main_hidden_count": main_hidden_count,
        "metrics": metrics,
        "subtitle": (
            f"观测 {observed_count} 个 · 本图展示 {gantt_displayed_count} 个"
            f" · 时间相对根任务起点{observation_subtitle}"
        ),
        "layout": layout.to_view(),
        "timeline_end_ms": timeline_end_ms,
        "px_per_ms": px_per_ms,
        "axis_ticks": axis_ticks,
        "grid_lines": grid_lines,
        "gantt_items": located_items,
        "unlocated_items": unlocated_items,
    }


def _create_sandbox(autoescape: bool = False) -> SandboxedEnvironment:
    """创建 Jinja2 sandbox 环境。

    SandboxedEnvironment 默认已限制危险操作。
    模板上下文只注入 event 对象，不暴露 Python 内置函数。
    """
    env = SandboxedEnvironment(
        loader=BaseLoader(),
        autoescape=autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.clear()
    env.globals["namespace"] = Namespace
    env.filters["inline_code"] = _render_inline_code
    return env


class _HTMLPolicyParser(HTMLParser):
    """检查模板和渲染 HTML 的危险标签与属性。"""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _FORBIDDEN_TAGS:
            raise ValueError(f"HTML 标签不允许: {tag}")
        lowered = {name.lower(): (value or "") for name, value in attrs}
        if any(name.startswith("on") for name in lowered):
            raise ValueError("HTML 事件属性不允许")
        if tag == "meta":
            http_equiv = lowered.get("http-equiv", "").lower()
            if http_equiv == "refresh":
                raise ValueError("meta refresh 不允许")
            if http_equiv == "content-security-policy":
                raise ValueError("不允许手动设置 CSP")
        for name, value in lowered.items():
            compact = re.sub(r"[\x00-\x20]+", "", value).lower()
            if "javascript:" in compact:
                raise ValueError("javascript URL 不允许")
            if name == "style" and _CSS_DANGEROUS.search(value):
                raise ValueError("危险 CSS 不允许")
            if name in {"src", "href", "action", "poster", "data"}:
                if _EXTERNAL_RESOURCE.search(compact):
                    raise ValueError("外部资源不允许")


def validate_html_policy(html: str) -> None:
    """执行严格 HTML/CSS/外部资源策略校验。"""
    if not isinstance(html, str):
        raise ValueError("HTML 必须是字符串")
    if _CSS_DANGEROUS.search(html):
        raise ValueError("危险 CSS 不允许")
    parser = _HTMLPolicyParser(convert_charrefs=True)
    parser.feed(html)
    parser.close()


def validate_html_template(template_str: str) -> None:
    """校验 HTML 模板策略并提前编译。"""
    validate_html_policy(template_str)
    _create_sandbox(autoescape=True).from_string(template_str)


def normalize_preview_event(event: Any) -> dict[str, Any]:
    """校验并复制 preview event，拒绝超限和敏感键。"""
    if not isinstance(event, dict):
        raise ValueError("event 必须是 JSON object")
    nodes = 0

    def walk(value: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PREVIEW_NODES:
            raise ValueError("event 节点数超过限制")
        if depth > MAX_PREVIEW_DEPTH:
            raise ValueError("event 深度超过限制")
        if isinstance(value, dict):
            if len(value) > MAX_PREVIEW_CONTAINER:
                raise ValueError("event 单容器元素数超过限制")
            result: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("event object key 必须是字符串")
                normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(marker in normalized_key for marker in _SENSITIVE_KEYS):
                    raise ValueError("event 包含敏感键")
                result[key] = walk(child, depth + 1)
            return result
        if isinstance(value, list):
            if len(value) > MAX_PREVIEW_CONTAINER:
                raise ValueError("event 单容器元素数超过限制")
            return [walk(child, depth + 1) for child in value]
        if isinstance(value, str):
            if len(value) > MAX_PREVIEW_STRING:
                raise ValueError("event 字符串超过限制")
            return value
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("event 数字必须是 finite")
            return value
        raise ValueError("event 包含非 JSON 值")

    normalized = walk(event, 0)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(canonical) > MAX_PREVIEW_EVENT_BYTES:
        raise ValueError("event canonical JSON 超过 64KiB")
    return normalized


def _inject_csp(html: str) -> str:
    match = re.search(r"<head(?:\s[^>]*)?>", html, re.I)
    if match:
        return html[: match.end()] + CSP_META + html[match.end() :]
    return CSP_META + html


def render_html_template(template_str: str, event: dict[str, Any]) -> str:
    """使用仅含 event 根变量的 sandbox 渲染并校验 HTML。"""
    validate_html_template(template_str)
    env = _create_sandbox(autoescape=True)
    result = env.from_string(template_str).render(event=event)
    if len(result.encode("utf-8")) > MAX_RENDERED_HTML_BYTES:
        raise ValueError("渲染后 HTML 超过 2MiB")
    validate_html_policy(result)
    return _inject_csp(result)


def render_preview(template_str: str, event: Any, canvas_width: Any) -> tuple[str, int]:
    """校验 preview 输入并返回不持久化的 HTML 与宽度。"""
    if isinstance(canvas_width, bool) or not isinstance(canvas_width, int):
        raise ValueError("canvas_width 必须是整数")
    if not 320 <= canvas_width <= 2048:
        raise ValueError("canvas_width 必须在 320..2048")
    normalized = normalize_preview_event(event)
    return render_html_template(template_str, normalized), canvas_width


def render_text(
    event: NormalizedEvent,
    template_str: str | None = None,
    display_context: DisplayContext | None = None,
) -> str:
    """使用 Jinja2 sandbox 将标准化事件渲染为纯文本。

    Args:
        event: 标准化事件对象。
        template_str: Jinja2 模板字符串，None 使用默认模板。

    Returns:
        渲染后的纯文本字符串。

    Raises:
        jinja2.exceptions.TemplateError: 模板渲染失败。
    """
    if template_str is None:
        template_str = DEFAULT_TEXT_TEMPLATE

    env = _create_sandbox()
    template = env.from_string(template_str)
    result = template.render(
        event=build_display_event_data(event.to_dict(), display_context=display_context)
    )
    return result


def render_text_default(
    event: NormalizedEvent, display_context: DisplayContext | None = None
) -> str:
    """使用默认模板渲染 OMP session_stop 事件为纯文本。

    与 FSD 中 OMP 示例格式保持一致。
    """
    data = build_display_event_data(event.to_dict(), display_context=display_context)
    lines: list[str] = []

    source_name = _get(data, ["source", "name"], "unknown")
    title = _get(data, ["title"], "事件")
    lines.append(f"[{source_name}] {title}")
    lines.append(
        f"状态：{_get(data, ['status_display'], _get(data, ['status'], '未知'))}"
    )
    lines.append("")

    summary = _get(data, ["summary"], "")
    if summary:
        lines.append(summary)

    for field in _get(data, ["fields"], []):
        label = _get(field, ["label"], "")
        value = _get(field, ["value"], "")
        if label or value:
            lines.append(f"{label}：{value}" if label else value)

    _append_subagent_timeline_text(lines, event)

    return "\n".join(lines)


def _append_subagent_timeline_text(
    lines: list[str], event: NormalizedEvent, *, max_items: int = 12
) -> None:
    """Append a bounded, non-visual summary for a root OpenCode completion."""

    timeline = event.subagent_timeline
    scope = getattr(event.session_scope, "value", event.session_scope)
    if (
        event.event != "opencode.session_idle"
        or scope != "root"
        or not isinstance(timeline, dict)
    ):
        return

    lines.append("")
    lines.append("子任务时间线：")
    observed = timeline.get("observedItemCount")
    displayed = timeline.get("displayedItemCount")
    if isinstance(observed, int) and isinstance(displayed, int):
        lines.append(f"任务数：{observed}（展示 {displayed}）")

    states: list[str] = []
    if timeline.get("partial") is True:
        states.append("部分数据")
    if timeline.get("truncated") is True:
        states.append("已截断")
    lines.append(f"状态：{'、'.join(states) if states else '完整'}")

    items = timeline.get("items")
    if not isinstance(items, list):
        return
    visible_items = [item for item in items if isinstance(item, dict)]
    for item in visible_items[:max_items]:
        name = _safe_timeline_display_text(item.get("name"))
        identity = _timeline_identity_from_item(item)
        if name:
            label = name
            if identity:
                label = f"{label}（{identity}）"
        elif identity:
            label = identity
        else:
            label = "子任务"

        status = item.get("status")
        status_key = status if isinstance(status, str) else ""
        status_text = {
            "running": "运行中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "unknown": "未知",
        }.get(status_key, "未知")
        detail = f"{label}：{status_text}"
        duration = item.get("durationMs")
        timing_quality = item.get("timingQuality")
        partial_reasons = timeline.get("partialReasons")
        clamped = isinstance(partial_reasons, list) and "clamped" in partial_reasons
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and timing_quality in {"observed", "fallback"}
            and not clamped
        ):
            detail += f"，耗时 {format_duration_ms(duration)}"
        elif timing_quality == "partial" or clamped:
            detail += "，区间不完整"
        lines.append(f"- {detail}")

    if len(visible_items) > max_items:
        lines.append(f"其余 {len(visible_items) - max_items} 个任务未展开")
    if (
        isinstance(observed, int)
        and isinstance(displayed, int)
        and observed > displayed
    ):
        lines.append(f"另有 {observed - displayed} 个任务未展示")


def prepare_subagent_timeline(event: NormalizedEvent) -> PreparedSubagentTimeline:
    """Create a request-local lazy timeline preparation shared by both images."""

    return PreparedSubagentTimeline(event)


def render_html_data(
    event: NormalizedEvent,
    display_context: DisplayContext | None = None,
    prepared_timeline: PreparedSubagentTimeline | None = None,
) -> dict[str, Any]:
    """为 HTML 模板准备数据 dict。

    基于 event.to_dict()，添加 HTML 模板所需的辅助字段
    （generated_at、event_time），并将 source 展平为字符串
    （兼容设计师模板对 event.source 的字符串预期）。

    Args:
        event: 标准化事件对象。

    Returns:
        包含 event 上下文的 dict：{"event": {...}}。
    """
    data = build_display_event_data(
        event.to_dict(),
        flatten_source=True,
        display_context=display_context,
    )

    # 添加辅助时间字段
    data["generated_at"] = format_timestamp(
        datetime.now(timezone.utc).isoformat(), display_context
    )
    data["event_time"] = format_timestamp(data.get("emitted_at", ""), display_context)
    timeline_view = (
        prepared_timeline.view
        if prepared_timeline is not None
        else _build_subagent_timeline_view(event)
    )
    if timeline_view is not None:
        data["subagent_timeline_view"] = timeline_view

    # NormalizedEvent 已用 list[dict] 存储 fields，模板 list 分支可用
    return {"event": data}


def render_html(
    event: NormalizedEvent,
    template_str: str | None = None,
    display_context: DisplayContext | None = None,
) -> str:
    """使用 Jinja2 sandbox 将标准化事件渲染为 HTML。

    生成的 HTML 可直接用于 AstrBot html_render / T2I 截图。

    Args:
        event: 标准化事件对象。
        template_str: Jinja2 HTML 模板字符串，None 使用 DEFAULT_HTML_TEMPLATE。

    Returns:
        渲染后的 HTML 字符串。

    Raises:
        jinja2.exceptions.TemplateError: 模板渲染失败。
    """
    if template_str is None:
        template_str = DEFAULT_HTML_TEMPLATE

    context = render_html_data(event, display_context)
    return render_html_template(template_str, context["event"])


def render_html_default(
    event: NormalizedEvent, display_context: DisplayContext | None = None
) -> str:
    """使用默认 HTML 模板将标准化事件渲染为 HTML 卡片。"""
    return render_html(event, DEFAULT_HTML_TEMPLATE, display_context)


def render_subagent_timeline(
    event: NormalizedEvent,
    prepared_timeline: PreparedSubagentTimeline | None = None,
) -> RenderedSubagentTimeline | None:
    """Render a complex timeline together with its deterministic screenshot layout."""

    view = (
        prepared_timeline.view
        if prepared_timeline is not None
        else _build_subagent_timeline_view(event)
    )
    if view is None or view["mode"] != "complex":
        return None
    layout = SubagentTimelineLayout.from_view(view["layout"])
    return RenderedSubagentTimeline(
        html=render_html_template(SUBAGENT_TIMELINE_HTML_TEMPLATE, view),
        layout=layout,
    )


def render_subagent_timeline_html(
    event: NormalizedEvent,
    prepared_timeline: PreparedSubagentTimeline | None = None,
) -> str | None:
    """Compatibility wrapper returning only the complex timeline HTML."""

    rendered = render_subagent_timeline(event, prepared_timeline)
    return rendered.html if rendered is not None else None


def validate_image_result(result: Any) -> bool:
    """校验图片渲染结果是否有效。

    支持的类型：
    - str URL（不下载校验，信任 AstrBot 图片组件）
    - str ``base64://...``
    - str ``data:image/...;base64,...``
    - str 本地文件路径
    - bytes（检查 PNG/JPEG/WebP magic number）

    Args:
        result: 图片渲染结果。

    Returns:
        True 表示校验通过。

    Raises:
        ValueError: 结果无效或无法识别。
        TypeError: 结果类型不支持。
    """
    if result is None:
        raise ValueError("image result is None")

    if isinstance(result, bytes):
        return _validate_image_bytes(result)

    if isinstance(result, str):
        result_str = result.strip()

        # base64:// 前缀 — 解码后校验
        if result_str.startswith("base64://"):
            import base64

            b64_data = result_str[len("base64://") :].strip()
            try:
                decoded = base64.b64decode(b64_data)
            except Exception as e:
                raise ValueError(f"base64 解码失败: {e}") from e
            return _validate_image_bytes(decoded)

        # data:image/...;base64,... — 解码后校验
        if result_str.startswith("data:"):
            if ";base64," in result_str:
                _, b64_part = result_str.split(";base64,", 1)
                import base64

                try:
                    decoded = base64.b64decode(b64_part.strip())
                except Exception as e:
                    raise ValueError(f"data URL base64 解码失败: {e}") from e
                return _validate_image_bytes(decoded)
            # data URL without base64 — 非标准，跳过校验
            return True

        # 本地文件路径 — 检查存在性
        import os

        if os.path.exists(result_str) and os.path.isfile(result_str):
            with open(result_str, "rb") as f:
                header = f.read(16)
            return _validate_image_bytes(header, is_header=True)
        elif result_str.startswith("http://") or result_str.startswith("https://"):
            # URL — 不下载校验
            return True
        elif os.path.isfile(result_str):
            # 其他路径（含相对路径）
            with open(result_str, "rb") as f:
                header = f.read(16)
            return _validate_image_bytes(header, is_header=True)
        else:
            # 非 URL 且非本地路径 — 尝试作为 base64 解码
            import base64

            try:
                decoded = base64.b64decode(result_str)
            except Exception:
                raise ValueError("无法识别的图片结果: 不是 URL、路径或 base64 编码")
            return _validate_image_bytes(decoded)

    raise TypeError(f"不支持的图片结果类型: {type(result).__name__}")


def _validate_image_bytes(data: bytes, is_header: bool = False) -> bool:
    """校验 bytes 是否为受支持的图片格式。

    检查 PNG（\\x89PNG）、JPEG（\\xff\\xd8\\xff）、WebP（RIFF....WEBP）magic number。

    Args:
        data: 图片 bytes 或文件头部 bytes。
        is_header: 如果 True，data 仅为文件头部（前 16 字节），
                    仍可进行 magic number 检查。

    Returns:
        True 表示匹配已知格式。

    Raises:
        ValueError: 格式不匹配或数据过短。
    """
    if not data:
        raise ValueError("图片数据为空")

    min_len = 3  # JPEG magic 最小长度
    if len(data) < min_len:
        raise ValueError(f"图片数据过短 ({len(data)} bytes)，无法校验 magic number")

    # PNG: \x89PNG\r\n\x1a\n (8 bytes)
    if data[:4] == b"\x89PNG":
        if len(data) >= 8:
            expected = b"\x89PNG\r\n\x1a\n"
            if data[:8] == expected:
                return True
        return True  # 前 4 字节匹配即视为 PNG

    # JPEG: \xff\xd8\xff (3 bytes)
    if data[:3] == b"\xff\xd8\xff":
        return True

    # WebP: RIFF....WEBP (12 bytes)
    if len(data) >= 4 and data[:4] == b"RIFF":
        if len(data) >= 12:
            if data[8:12] == b"WEBP":
                return True
            raise ValueError("RIFF 文件头但非 WEBP 格式")
        # 头部不够 12 字节，仅匹配 RIFF 则视为可能 WebP
        return True

    raise ValueError(f"不支持的图片格式: magic={data[:8].hex()} (支持 PNG/JPEG/WebP)")


def trim_viewport_whitespace(
    image_result: Any,
    canvas_width: int = 812,
    card_width: int = HTML_CARD_WIDTH,
    body_padding: int = HTML_BODY_PADDING,
) -> Any:
    """裁切 HTML 卡片截图视口中右侧/底部多余背景空白。

    检测有效内容区域后直接裁剪保存，不再二次创建新画布。
    页面背景已改为纯白（``#ffffff``），卡片本身自带圆角阴影，
    不再需要额外归一化画布。

    仅处理本地文件路径；URL、base64、data URL、bytes 原样返回。
    修改在原文件上就地执行（保存到临时文件后 os.replace）。

    Args:
        image_result: html_render 返回的图片结果。
        canvas_width: 渲染时使用的视口宽度（CSS 像素），用于推算缩放比。
        card_width: 当前卡片的 CSS 内容宽度。
        body_padding: 卡片左侧的 CSS body padding。

    Returns:
        原 image_result（就地修改或原样返回）。
    """
    if not (isinstance(image_result, str) and os.path.isfile(image_result)):
        return image_result

    temp_path = f"{image_result}.trim"
    try:
        from PIL import Image

        with Image.open(image_result) as img:
            if img.width < 360 or img.height < 240:
                return image_result

            crop_box = _detect_trim_box(
                img,
                canvas_width,
                card_width=card_width,
                body_padding=body_padding,
            )
            if crop_box is None:
                return image_result

            cropped = img.crop(crop_box)
            fmt = (img.format or "PNG").upper()
            save_kwargs: dict[str, Any] = {}
            if fmt in ("JPEG", "JPG"):
                fmt = "JPEG"
                cropped = cropped.convert("RGB")
                save_kwargs = {"quality": 95, "optimize": True}
            elif fmt == "PNG":
                save_kwargs = {"optimize": True}

            cropped.save(temp_path, format=fmt, **save_kwargs)

        # 验证并替换原文件（最小尺寸 256 bytes，有效 PNG/JPEG 头部即视为有效）
        if (
            os.path.exists(temp_path)
            and os.path.getsize(temp_path) > 256
            and _validate_temp_image(temp_path)
        ):
            os.replace(temp_path, image_result)
            _log_debug(f"trim_viewport_whitespace: 已裁切为 {crop_box}")
        elif os.path.exists(temp_path):
            os.remove(temp_path)
    except ImportError:
        # PIL 不可用，静默跳过
        pass
    except Exception as e:
        # 清理临时文件
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        _log_debug(f"trim_viewport_whitespace 跳过: {e}")

    return image_result


def _detect_trim_box(
    image: Any,
    canvas_width: int,
    card_width: int = HTML_CARD_WIDTH,
    body_padding: int = HTML_BODY_PADDING,
) -> tuple[int, int, int, int] | None:
    """识别 HTML 卡片截图中左上对齐卡片内容的有效区域。

    右侧按已知画布宽度推算卡片右边缘（view - 右 padding）；
    底部使用像素差异检测。参考 astrbot_plugin_bilibili 的 _detect_card_crop_box。

    Args:
        image: PIL Image 对象。
        canvas_width: 渲染时的浏览器视口 CSS 宽度（如 812）。

    Returns:
        (left, top, right, bottom) 裁剪框，无需处理时返回 None。
    """
    width, height = image.size

    # --- 右侧裁剪 ---
    # 默认通知卡片为 780px；专用卡片可传入自己的宽度和 body padding。
    # 若 T2I 尊重 viewport_width，截图宽度约为 canvas_width * scale；
    # 若旧服务忽略 viewport_width，则常见截图宽度约为 1280 * scale。
    expected_right = _expected_canvas_right(
        width,
        canvas_width,
        card_width=card_width,
        body_padding=body_padding,
    )

    crop_right = width
    right_margin = _scaled_right_crop_padding(width, canvas_width)
    candidate_right = min(width, expected_right + right_margin)
    if width - candidate_right > max(8, int(width * 0.006)):
        crop_right = candidate_right

    # --- 底部裁剪（像素差异检测） ---
    rgb = image.convert("RGB")
    bottom = _find_content_limit(rgb, axis="y")

    crop_bottom = height
    if bottom is not None:
        bottom_margin = max(18, int(height * 0.018))
        candidate = min(height, bottom + bottom_margin)
        if height - candidate > max(24, int(height * 0.03)):
            crop_bottom = candidate

    if crop_right == width and crop_bottom == height:
        return None
    return (0, 0, crop_right, crop_bottom)


def _expected_canvas_right(
    image_width: int,
    canvas_width: int,
    card_width: int = HTML_CARD_WIDTH,
    body_padding: int = HTML_BODY_PADDING,
) -> int:
    """根据截图宽度推断画布右边界，兼容 viewport_width 生效/未生效两类情况。"""
    content_right_css = max(body_padding + card_width, 1)
    scale = _infer_device_scale(image_width, canvas_width)
    return int(content_right_css * scale)


def _scaled_right_crop_padding(image_width: int, canvas_width: int) -> int:
    """将右侧视觉裁剪留白转换到截图像素。

    右侧存在卡片阴影和圆角溢出，若保留完整 body padding，视觉上会比左侧更宽。
    因此右侧只保留略小于 body padding 的视觉 padding，用于平衡阴影带来的视觉宽度。
    """
    return max(
        0,
        int(RIGHT_VISUAL_CROP_PADDING * _infer_device_scale(image_width, canvas_width)),
    )


def _infer_device_scale(image_width: int, canvas_width: int) -> float:
    """推断截图设备缩放，兼容 viewport_width 生效和旧服务默认 1280 视口。"""
    viewport_width = max(canvas_width, 1)
    configured_scale = image_width / viewport_width
    fallback_scale = image_width / DEFAULT_FALLBACK_VIEWPORT_WIDTH

    configured_match = _nearest_known_device_scale(configured_scale)
    fallback_match = _nearest_known_device_scale(fallback_scale)
    if configured_match and fallback_match:
        configured_known, configured_diff = configured_match
        fallback_known, fallback_diff = fallback_match
        return fallback_known if fallback_diff < configured_diff else configured_known
    if configured_match:
        return configured_match[0]
    if fallback_match:
        return fallback_match[0]

    return configured_scale


def _is_known_device_scale(scale: float) -> bool:
    return _nearest_known_device_scale(scale) is not None


def _nearest_known_device_scale(scale: float) -> tuple[float, float] | None:
    nearest = min(
        ((candidate, abs(scale - candidate)) for candidate in DEVICE_SCALE_CANDIDATES),
        key=lambda item: item[1],
    )
    return nearest if nearest[1] <= 0.08 else None


def _find_content_limit(image: Any, axis: str) -> int | None:
    """在指定轴上找到最后一个明显不是纯背景的边缘位置。

    通过采样相邻像素通道差异来定位内容边界。
    axis='x' 从右向左找右侧边界，axis='y' 从下向上找底部边界。
    """
    width, height = image.size
    primary = width if axis == "x" else height
    secondary = height if axis == "x" else width
    primary_step = max(1, primary // 900)
    secondary_step = max(2, secondary // 280)

    scores: list[tuple[int, float]] = []
    for pos in range(0, primary - primary_step, primary_step):
        total = 0.0
        count = 0
        for cross in range(0, secondary, secondary_step):
            if axis == "x":
                px1 = image.getpixel((pos, cross))
                px2 = image.getpixel((pos + primary_step, cross))
            else:
                px1 = image.getpixel((cross, pos))
                px2 = image.getpixel((cross, pos + primary_step))
            total += _pixel_distance(px1, px2)
            count += 1
        if count:
            scores.append((pos, total / count))

    if not scores:
        return None

    # 取尾部 18% 的中位数作为背景噪声基线
    tail_start = int(len(scores) * 0.82)
    tail_scores = sorted(score for _, score in scores[tail_start:]) or [0.0]
    background_score = tail_scores[len(tail_scores) // 2]
    threshold = max(5.0, background_score * 3.2 + 1.5)

    for pos, score in reversed(scores):
        if score >= threshold:
            return pos + primary_step
    return None


def _pixel_distance(px1: Any, px2: Any) -> float:
    """计算两个 RGB 像素的平均通道差。"""
    return (
        abs(int(px1[0]) - int(px2[0]))
        + abs(int(px1[1]) - int(px2[1]))
        + abs(int(px1[2]) - int(px2[2]))
    ) / 3


def _validate_temp_image(img_path: str) -> bool:
    """使用 PIL verify 验证临时图片文件有效。"""
    try:
        from PIL import Image

        with Image.open(img_path) as img:
            img.verify()
        return True
    except Exception:
        return False


def _log_debug(msg: str) -> None:
    """尝试使用 astrbot logger 输出 debug 日志，不可用时静默跳过。"""
    try:
        from astrbot.api import logger

        logger.debug(msg)
    except Exception:
        pass


def _get(obj: Any, keys: list[str], default: Any = None) -> Any:
    """安全地从嵌套字典中获取值。"""
    current = obj
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
    if current is None or (isinstance(current, str) and not current):
        return default
    return current
