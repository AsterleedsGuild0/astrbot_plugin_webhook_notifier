const ENDPOINTS = Object.freeze({
  baseUrl: 'base-url',
  templates: 'templates',
  save: 'templates/save',
  apply: 'templates/apply',
  delete: 'templates/delete',
  preview: 'templates/preview',
})

export class BridgeUnavailableError extends Error {
  constructor() {
    super('必须从AstrBot插件详情页打开')
    this.name = 'BridgeUnavailableError'
  }
}

export async function connectBridge() {
  const bridge = window.AstrBotPluginPage
  if (
    !bridge ||
    typeof bridge.apiGet !== 'function' ||
    typeof bridge.apiPost !== 'function'
  ) {
    throw new BridgeUnavailableError()
  }

  const context = typeof bridge.ready === 'function' ? await bridge.ready() : {}
  return { bridge, context: context && typeof context === 'object' ? context : {} }
}

export function createTemplateApi(bridge) {
  return {
    getBaseUrl() {
      return bridge.apiGet(ENDPOINTS.baseUrl)
    },
    listTemplates() {
      return bridge.apiGet(ENDPOINTS.templates)
    },
    getTemplate(id) {
      return bridge.apiGet(`templates/${encodeURIComponent(id)}`)
    },
    saveTemplate(payload) {
      return bridge.apiPost(ENDPOINTS.save, payload)
    },
    applyTemplate(payload) {
      return bridge.apiPost(ENDPOINTS.apply, payload)
    },
    deleteTemplate(payload) {
      return bridge.apiPost(ENDPOINTS.delete, payload)
    },
    previewTemplate(payload) {
      return bridge.apiPost(ENDPOINTS.preview, payload)
    },
  }
}

export function describeApiError(error, action = '操作') {
  const message = error instanceof Error ? error.message : String(error)
  const status = error && typeof error === 'object' ? error.status ?? error.statusCode : null
  const isConflict = status === 409 || /(^|\D)409(\D|$)|conflict|revision/i.test(message)

  if (isConflict) {
    return `版本冲突：模板已在其他位置更新。请重新载入后再${action}。`
  }
  return `${action}失败：${message || '未知错误'}`
}
