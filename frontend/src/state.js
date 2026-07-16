export const CANVAS_MIN = 320
export const CANVAS_MAX = 2048

export function createAppState() {
  return {
    templates: [],
    activeId: null,
    effectiveActiveId: null,
    sampleEvent: {},
    defaultEventText: '{}',
    current: null,
    busy: null,
    initialized: false,
  }
}

export function normalizeTemplateId(value) {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object' && typeof value.id === 'string') return value.id
  return null
}

export function createCurrentTemplate(detail, overrides = {}) {
  const current = {
    id: detail?.id ?? null,
    displayName: detail?.display_name ?? detail?.displayName ?? '',
    content: detail?.content ?? '',
    canvasWidth: Number(detail?.canvas_width ?? detail?.canvasWidth ?? 812),
    builtIn: Boolean(detail?.built_in ?? detail?.builtIn),
    revision: detail?.revision ?? null,
    valid: detail?.valid !== false,
    active: Boolean(detail?.active),
    ...overrides,
    originalSnapshot: null,
  }

  if (current.id !== null) {
    current.originalSnapshot = serializeEditable(current)
  }
  return current
}

export function serializeEditable(current) {
  return JSON.stringify({
    displayName: current.displayName,
    content: current.content,
    canvasWidth: Number(current.canvasWidth),
  })
}

export function isDirty(current) {
  if (!current) return false
  if (current.id === null || current.originalSnapshot === null) return true
  return serializeEditable(current) !== current.originalSnapshot
}

export function markSaved(current) {
  current.originalSnapshot = serializeEditable(current)
}

export function validateCurrent(current) {
  if (!current) return '请先选择模板。'
  if (!current.displayName.trim()) return '请输入模板名称。'

  const width = Number(current.canvasWidth)
  if (!Number.isInteger(width) || width < CANVAS_MIN || width > CANVAS_MAX) {
    return `画布宽度必须是 ${CANVAS_MIN} 到 ${CANVAS_MAX} 之间的整数。`
  }
  return null
}

export function toTemplateSummary(current, responseTemplate = {}) {
  return {
    id: responseTemplate.id ?? current.id,
    display_name: responseTemplate.display_name ?? current.displayName,
    canvas_width: responseTemplate.canvas_width ?? Number(current.canvasWidth),
    built_in: responseTemplate.built_in ?? current.builtIn,
    revision: responseTemplate.revision ?? current.revision,
    active: responseTemplate.active ?? current.active,
    valid: responseTemplate.valid ?? current.valid,
    error: responseTemplate.error,
  }
}

export function upsertTemplate(templates, summary) {
  const index = templates.findIndex((item) => item.id === summary.id)
  if (index === -1) return [...templates, summary]
  return templates.map((item, itemIndex) => (itemIndex === index ? { ...item, ...summary } : item))
}

export function prettyJson(value) {
  return JSON.stringify(value ?? {}, null, 2)
}
