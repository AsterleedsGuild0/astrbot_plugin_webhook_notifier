/** 出站内容的公共清洗与匿名化：只产出白名单字段需要的有界文本。 */
import { createHmac, randomBytes } from "node:crypto";
import type { AskOption, AskQuestion, InteractionDetail } from "./wire.ts";

/** 进程内随机盐：同一进程的多个会话工厂共享，用于匿名关联会话与代理身份。 */
const salt = randomBytes(32);

export function ref(): string { return randomBytes(16).toString("hex"); }
export function anonymous(value: string): string { return createHmac("sha256", salt).update(value).digest("hex").slice(0, 32); }
export const ABSOLUTE_OR_URL = /(?:^|\s|\/)(?:[A-Za-z]:[\\/]|[~\\/])|https?:\/\//;
/**
 * 名称与交互内容的公共清洗：去控制字符、压缩空白；URL 和绝对／用户目录路径整段替换为 …，
 * 相对路径（如 `core/omp.py`）保留。模型名不做替换，含路径即舍弃。
 */
export function text(value: unknown, limit = 96, model = false): string | undefined {
  if (typeof value !== "string") return;
  let clean = value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\s+/gu, " ").replace(/[\p{Cc}\p{Cf}]/gu, "").trim();
  if (!model) clean = clean.replace(/https?:\/\/\S+/g, "…").replace(/(^|\s)(?:[A-Za-z]:[\\/]|~[\\/]|[\\/])\S*/g, "$1…").trim();
  if (!clean || /^[…\s]+$/.test(clean) || ABSOLUTE_OR_URL.test(clean)) return;
  return Array.from(clean).slice(0, limit).join("").trim() || undefined;
}
/** ask 与 OpenCode 问题卡一致：发送题干、标题、选项标签、描述和推荐项；最多 4 题、每题 8 个选项。不读取答案。 */
export function askDetail(input: unknown): InteractionDetail | undefined {
  const raw = (input as { questions?: unknown } | undefined)?.questions;
  if (!Array.isArray(raw) || !raw.length) return;
  const questions: AskQuestion[] = [];
  for (const item of raw.slice(0, 4) as { question?: unknown; header?: unknown; options?: unknown; recommended?: unknown }[]) {
    const questionText = text(item?.question, 300);
    if (!questionText) continue;
    const header = text(item.header, 40);
    const options: AskOption[] = [];
    if (Array.isArray(item.options)) item.options.slice(0, 8).forEach((option, index) => {
      const label = text((option as { label?: unknown } | undefined)?.label, 80);
      if (!label || options.some(existing => existing.label === label)) return;
      const description = text((option as { description?: unknown }).description, 200);
      options.push({ label, ...(description ? { description } : {}), ...(item.recommended === index ? { recommended: true as const } : {}) });
    });
    questions.push({ text: questionText, ...(header ? { header } : {}), ...(options.length ? { options } : {}) });
  }
  return questions.length ? { questionCount: Math.min(raw.length, 99), questions } : undefined;
}
