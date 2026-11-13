/** 出站内容的公共清洗与匿名化：只产出白名单字段需要的有界文本。 */
import { createHmac, randomBytes } from "node:crypto";
import type { ChoiceField, FormField, FormSpan, HtmlForm } from "@oh-my-pi/pi-tui/html-form/parse";
import type { AskOption, AskQuestion, InteractionDetail } from "./wire.ts";

let htmlForm: {
  parseHtmlForm: (html: string) => HtmlForm;
  spansText: (spans: readonly FormSpan[]) => string;
  fieldQuestions: (form: HtmlForm) => Map<FormField, string>;
  isArrayChoice: (field: ChoiceField) => boolean;
} | undefined;
try {
  const [{ parseHtmlForm, spansText }, { fieldQuestions, isArrayChoice }] = await Promise.all([
    import("@oh-my-pi/pi-tui/html-form/parse"),
    import("@oh-my-pi/pi-tui/html-form/data"),
  ]);
  htmlForm = { parseHtmlForm, spansText, fieldQuestions, isArrayChoice };
} catch { /* OMP < 18.18.3 或无 pi-tui 的纯回环环境：保留 questions[] 路径。 */ }
export const hasHtmlForm = htmlForm !== undefined;

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
/** ask 与 OpenCode 问题卡一致：兼容 OMP < 18.18.3 的 questions[] 与 >= 18.18.3 的 HTML 表单；最多 4 题、每题 8 个选项，不读取答案。 */
export function askDetail(input: unknown): InteractionDetail | undefined {
  if (typeof input !== "object" || input === null) return;
  const raw = "questions" in input ? input.questions : undefined;
  if (Array.isArray(raw)) {
    if (!raw.length) return;
    const questions: AskQuestion[] = [];
    for (const item of raw.slice(0, 4)) {
      if (typeof item !== "object" || item === null) continue;
      const questionText = text("question" in item ? item.question : undefined, 300);
      if (!questionText) continue;
      const header = text("header" in item ? item.header : undefined, 40);
      const multi = "multi" in item && item.multi === true;
      const options: AskOption[] = [];
      const rawOptions = "options" in item ? item.options : undefined;
      const recommended = "recommended" in item ? item.recommended : undefined;
      if (Array.isArray(rawOptions)) rawOptions.slice(0, 8).forEach((option, index) => {
        if (typeof option !== "object" || option === null) return;
        const label = text("label" in option ? option.label : undefined, 80);
        if (!label || options.some(existing => existing.label === label)) return;
        const description = text("description" in option ? option.description : undefined, 200);
        options.push({ label, ...(description ? { description } : {}), ...(recommended === index ? { recommended: true as const } : {}) });
      });
      questions.push({ text: questionText, ...(header ? { header } : {}), ...(multi ? { multi: true as const } : {}), ...(options.length ? { options } : {}) });
    }
    return questions.length ? { questionCount: Math.min(raw.length, 99), questions } : undefined;
  }
  const html = "html" in input ? input.html : undefined;
  if (typeof html !== "string" || !html.trim() || !htmlForm) return;
  try {
    const form = htmlForm.parseHtmlForm(html);
    const named = form.fields.filter(field => Boolean(field.name));
    if (!named.length) return;
    const labels = htmlForm.fieldQuestions(form);
    const questions: AskQuestion[] = [];
    for (const field of named.slice(0, 4)) {
      const questionText = text(labels.get(field) ?? field.name, 300);
      if (!questionText) continue;
      if (field.kind !== "choice") {
        questions.push({ text: questionText });
        continue;
      }
      const enabled = field.options.filter(option => !option.disabled);
      const checked = enabled.filter(option => option.checked);
      const recommendedOption = checked.length === 1 ? checked[0] : undefined;
      const options: AskOption[] = [];
      for (const option of enabled) {
        if (options.length >= 8) break;
        const label = text(htmlForm.spansText(option.label), 80);
        if (!label || options.some(existing => existing.label === label)) continue;
        options.push({ label, ...(option === recommendedOption ? { recommended: true as const } : {}) });
      }
      const multi = htmlForm.isArrayChoice(field);
      questions.push({ text: questionText, ...(multi ? { multi: true as const } : {}), ...(options.length ? { options } : {}) });
    }
    return questions.length ? { questionCount: Math.min(named.length, 99), questions } : undefined;
  } catch {
    return;
  }
}
