import './style.css'
import { BridgeUnavailableError, connectBridge, createTemplateApi, describeApiError } from './api'
import { createConfirmAction } from './confirm'
import {
  CANVAS_MAX,
  CANVAS_MIN,
  createAppState,
  createCurrentTemplate,
  isDirty,
  markSaved,
  normalizeTemplateId,
  prettyJson,
  toTemplateSummary,
  upsertTemplate,
  validateCurrent,
} from './state'
import {
  createMonacoEditors,
  onWorkerDiagnostics,
  runWorkerDiagnostics,
  setMonacoTheme,
} from './monaco'

const state = createAppState()
const detailCache = new Map()
let api = null
let editors = null
let previewTimer = null
let previewVersion = 0
let previewBusyVersion = null
let previewCanvasWidth = null
let previewResizeObserver = null
let noticeTimer = null
let baseUrl = null
let baseUrlConfigured = null
let baseUrlStatus = 'loading'
let copyFeedbackTimer = null

const elements = Object.fromEntries(
  [
    'bridge-gate',
    'gate-title',
    'gate-message',
    'retry-button',
    'page-content',
    'notice-region',
    'base-url-panel',
    'base-url-config-status',
    'base-url-display',
    'base-url-value',
    'base-url-state-text',
    'base-url-warning',
    'copy-base-url-button',
    'copy-base-url-label',
    'retry-base-url-button',
    'worker-status',
    'worker-status-text',
    'template-list',
    'template-empty',
    'new-template-button',
    'copy-template-button',
    'delete-template-button',
    'delete-template-label',
    'delete-template-help',
    'template-name',
    'canvas-width',
    'dirty-indicator',
    'dirty-label',
    'revision-label',
    'save-button',
    'apply-button',
    'save-apply-button',
    'field-error',
    'editor-mode-label',
    'template-editor',
    'preview-width-label',
    'refresh-preview-button',
    'preview-stage',
    'preview-placeholder',
    'preview-state-title',
    'preview-state-message',
    'preview-canvas',
    'preview-frame',
    'preview-data-panel',
    'event-editor',
    'reset-event-button',
    'json-error',
    'confirm-overlay',
    'confirm-dialog',
    'confirm-title',
    'confirm-message',
    'confirm-cancel-button',
    'confirm-submit-button',
  ].map((id) => [id, document.getElementById(id)]),
)

const confirmAction = createConfirmAction({
  overlay: elements['confirm-overlay'],
  dialog: elements['confirm-dialog'],
  title: elements['confirm-title'],
  message: elements['confirm-message'],
  cancelButton: elements['confirm-cancel-button'],
  confirmButton: elements['confirm-submit-button'],
})

function setTheme(context = {}) {
  const hostTheme = document.documentElement.dataset.theme
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches
  const theme = context.isDark === true || hostTheme === 'dark' || (!hostTheme && prefersDark) ? 'dark' : 'light'
  document.documentElement.dataset.theme = theme
  setMonacoTheme(theme)
}

function showGate({ title, message, retry = false, loading = false }) {
  elements['gate-title'].textContent = title
  elements['gate-message'].textContent = message
  elements['retry-button'].classList.toggle('is-hidden', !retry)
  elements['bridge-gate'].classList.toggle('is-loading', loading)
  elements['bridge-gate'].classList.remove('is-hidden')
  elements['page-content'].classList.add('is-hidden')
}

function showWorkspace() {
  elements['bridge-gate'].classList.add('is-hidden')
  elements['page-content'].classList.remove('is-hidden')
}

function showNotice(message, tone = 'success') {
  window.clearTimeout(noticeTimer)
  elements['notice-region'].textContent = message
  elements['notice-region'].dataset.tone = tone
  elements['notice-region'].classList.add('is-visible')
  noticeTimer = window.setTimeout(() => {
    elements['notice-region'].classList.remove('is-visible')
  }, tone === 'error' ? 6500 : 3500)
}

function renderBaseUrl() {
  const ready = baseUrlStatus === 'ready' && typeof baseUrl === 'string'
  const display = elements['base-url-display']
  const value = elements['base-url-value']
  const stateText = elements['base-url-state-text']
  const configStatus = elements['base-url-config-status']

  elements['base-url-panel'].dataset.state = baseUrlStatus
  display.dataset.state = baseUrlStatus
  value.classList.toggle('is-hidden', !ready)
  stateText.classList.toggle('is-hidden', ready)
  elements['copy-base-url-button'].disabled = !ready
  elements['retry-base-url-button'].classList.toggle(
    'is-hidden',
    !['empty', 'error'].includes(baseUrlStatus),
  )
  elements['base-url-warning'].classList.toggle(
    'is-hidden',
    !(ready && baseUrlConfigured === false),
  )

  if (ready) {
    value.textContent = baseUrl
    stateText.textContent = ''
    configStatus.textContent = baseUrlConfigured ? '已配置' : '本地监听'
    configStatus.dataset.state = baseUrlConfigured ? 'configured' : 'local'
    return
  }

  value.textContent = ''
  const states = {
    loading: ['正在读取 Base URL…', '正在读取'],
    empty: ['后端未返回可用的 Base URL。请联系管理员检查插件配置。', '暂无地址'],
    error: ['无法读取 Base URL，请稍后重试。', '读取失败'],
  }
  const [message, label] = states[baseUrlStatus] ?? states.error
  stateText.textContent = message
  configStatus.textContent = label
  configStatus.dataset.state = baseUrlStatus
}

async function loadBaseUrl() {
  baseUrl = null
  baseUrlConfigured = null
  baseUrlStatus = 'loading'
  renderBaseUrl()

  try {
    const result = await api.getBaseUrl()
    if (
      !result ||
      typeof result.base_url !== 'string' ||
      result.base_url.trim() === '' ||
      typeof result.configured !== 'boolean'
    ) {
      baseUrlStatus = 'empty'
      return
    }
    baseUrl = result.base_url
    baseUrlConfigured = result.configured
    baseUrlStatus = 'ready'
  } catch {
    baseUrlStatus = 'error'
  } finally {
    renderBaseUrl()
  }
}

function selectBaseUrlText() {
  const selection = window.getSelection?.()
  if (!selection || !elements['base-url-value'].textContent) return
  const range = document.createRange()
  range.selectNodeContents(elements['base-url-value'])
  selection.removeAllRanges()
  selection.addRange(range)
}

function copyWithFallback(value) {
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.setAttribute('aria-hidden', 'true')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  document.body.append(textarea)
  textarea.select()
  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }
  textarea.remove()
  return copied
}

async function copyBaseUrl() {
  if (baseUrlStatus !== 'ready' || typeof baseUrl !== 'string') return

  let copied = false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(baseUrl)
      copied = true
    } else {
      copied = copyWithFallback(baseUrl)
    }
  } catch {
    copied = copyWithFallback(baseUrl)
  }

  if (!copied) {
    selectBaseUrlText()
    showNotice('复制失败，请手动复制已选中的 Base URL。', 'error')
    return
  }

  window.clearTimeout(copyFeedbackTimer)
  elements['copy-base-url-label'].textContent = '已复制'
  showNotice('已复制')
  copyFeedbackTimer = window.setTimeout(() => {
    elements['copy-base-url-label'].textContent = '复制'
  }, 2000)
}

function setBusy(operation) {
  state.busy = operation
  document.body.dataset.busy = operation ?? ''
  renderControls()
}

function updateActiveFlags() {
  state.templates = state.templates.map((template) => ({
    ...template,
    active: template.id === state.activeId,
  }))
  if (state.current) state.current.active = state.current.id === state.activeId
}

function renderTemplateList() {
  elements['template-list'].replaceChildren()
  elements['template-empty'].classList.toggle('is-hidden', state.templates.length > 0)

  for (const template of state.templates) {
    const selected = state.current?.id === template.id
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'template-item'
    button.dataset.templateId = template.id
    button.setAttribute('role', 'option')
    button.setAttribute('aria-selected', String(selected))
    if (selected) button.classList.add('is-selected')

    const header = document.createElement('span')
    header.className = 'template-item-header'
    const name = document.createElement('strong')
    name.textContent = template.display_name
    header.append(name)

    if (template.id === state.activeId) header.append(createBadge('已应用', 'active'))
    if (template.id === state.effectiveActiveId && template.id !== state.activeId) {
      header.append(createBadge('实际生效', 'effective'))
    }

    const meta = document.createElement('span')
    meta.className = 'template-item-meta'
    meta.textContent = `${template.canvas_width}px · ${template.built_in ? '内置只读' : `修订 ${template.revision ?? '—'}`}`
    button.append(header, meta)

    if (template.valid === false) {
      button.append(createBadge(template.error ? '模板异常' : '无效模板', 'error'))
      if (template.error) button.title = template.error
    }
    button.addEventListener('click', () => selectTemplate(template.id))
    elements['template-list'].append(button)
  }
}

function createBadge(text, tone) {
  const badge = document.createElement('span')
  badge.className = `template-badge template-badge-${tone}`
  badge.textContent = text
  return badge
}

function renderControls() {
  const current = state.current
  const busy = Boolean(state.busy)
  const dirty = isDirty(current)
  const readOnly = Boolean(current?.builtIn)
  const saved = Boolean(current?.id)
  const validationError = current ? validateCurrent(current) : null

  elements['template-name'].disabled = !current || readOnly || busy
  elements['canvas-width'].disabled = !current || readOnly || busy
  elements['new-template-button'].disabled = busy || !state.templates.some((item) => item.built_in)
  elements['copy-template-button'].disabled = busy || !current
  elements['save-button'].disabled = busy || !current || readOnly || !dirty || Boolean(validationError)
  elements['apply-button'].disabled = busy || !current || !saved || dirty || current.id === state.activeId
  elements['save-apply-button'].disabled = busy || !current || readOnly || Boolean(validationError)
  elements['reset-event-button'].disabled = busy

  elements['dirty-indicator'].dataset.dirty = String(dirty)
  elements['refresh-preview-button'].disabled = busy || previewBusyVersion !== null || !current
  elements['dirty-label'].textContent = dirty ? '未保存' : '已保存'
  elements['revision-label'].textContent = current?.revision ? `修订 ${current.revision}` : current ? '新模板' : ''
  elements['editor-mode-label'].textContent = readOnly ? '内置模板 · 只读' : '可编辑'
  editors?.setTemplateReadOnly(readOnly || busy)
  renderDeleteControl(current, busy)
}

function renderDeleteControl(current, busy) {
  const button = elements['delete-template-button']
  const label = elements['delete-template-label']
  const help = elements['delete-template-help']

  button.disabled = busy || !current || Boolean(current?.builtIn)
  button.classList.toggle('is-discard-action', Boolean(current && current.id === null))

  if (!current) {
    label.textContent = '删除模板'
    button.title = '请先选择模板'
    help.textContent = '选择模板后可查看删除条件。'
    return
  }
  if (current.builtIn) {
    label.textContent = '内置模板'
    button.title = '内置模板不可删除'
    help.textContent = '内置模板不可删除，可复制后再编辑。'
    return
  }
  if (current.id === null) {
    label.textContent = '放弃草稿'
    button.title = '放弃未保存草稿'
    help.textContent = '草稿尚未保存，可直接放弃。'
    return
  }
  if (current.id === state.activeId) {
    label.textContent = '切换并删除'
    button.title = '先应用内置模板，再删除当前模板'
    help.textContent = '当前模板正在应用；删除时将自动切换到内置模板。'
    return
  }

  label.textContent = '删除模板'
  button.title = '删除当前模板'
  help.textContent = '仅删除当前自定义模板，操作无法撤销。'
}

function renderCurrent({ replaceEditor = false } = {}) {
  const current = state.current
  if (!current) {
    elements['template-name'].value = ''
    elements['canvas-width'].value = ''
    if (replaceEditor) editors.setTemplate('', false)
    renderTemplateList()
    renderControls()
    return
  }

  elements['template-name'].value = current.displayName
  elements['canvas-width'].value = String(current.canvasWidth)
  if (replaceEditor) editors.setTemplate(current.content, current.builtIn)
  renderTemplateList()
  renderControls()
}

async function confirmDiscard(action) {
  if (!isDirty(state.current)) return true
  return confirmAction({
    title: '放弃未保存修改？',
    message: `当前模板有未保存修改。继续${action}将放弃这些修改。`,
    confirmLabel: '放弃并继续',
    danger: true,
  })
}

async function loadTemplate(id) {
  if (detailCache.has(id)) return detailCache.get(id)
  const detail = await api.getTemplate(id)
  detailCache.set(id, detail)
  return detail
}

async function selectTemplate(id, { skipGuard = false, force = false } = {}) {
  if ((state.busy && !force) || state.current?.id === id) return
  if (!skipGuard && !(await confirmDiscard('切换模板'))) return

  setBusy('load-template')
  clearFieldError()
  try {
    const detail = await loadTemplate(id)
    state.current = createCurrentTemplate(detail, { active: id === state.activeId })
    renderCurrent({ replaceEditor: true })
    schedulePreview()
  } catch (error) {
    showNotice(describeApiError(error, '载入模板'), 'error')
  } finally {
    setBusy(null)
  }
}

async function createNewTemplate() {
  if (state.busy) return
  if (!(await confirmDiscard('新建模板'))) return
  const base = state.templates.find((template) => template.built_in)
  if (!base) {
    showNotice('无法新建：没有可用的内置模板。', 'error')
    return
  }

  setBusy('new-template')
  try {
    const detail = await loadTemplate(base.id)
    state.current = createCurrentTemplate(detail, {
      id: null,
      displayName: '新模板',
      builtIn: false,
      revision: null,
      active: false,
      originalSnapshot: null,
    })
    renderCurrent({ replaceEditor: true })
    editors.focusTemplate()
    schedulePreview()
  } catch (error) {
    showNotice(describeApiError(error, '新建模板'), 'error')
  } finally {
    setBusy(null)
  }
}

async function copyCurrentTemplate() {
  if (state.busy || !state.current) return
  if (!(await confirmDiscard('复制模板'))) return
  state.current = createCurrentTemplate(state.current, {
    id: null,
    displayName: `${state.current.displayName} 副本`,
    builtIn: false,
    revision: null,
    active: false,
    originalSnapshot: null,
  })
  renderCurrent({ replaceEditor: true })
  elements['template-name'].focus()
  elements['template-name'].select()
  schedulePreview()
}

async function saveCurrent(apply) {
  const current = state.current
  if (!current || current.builtIn || state.busy) return
  const validationError = validateCurrent(current)
  if (validationError) {
    showFieldError(validationError)
    return
  }

  setBusy(apply ? 'save-apply' : 'save')
  clearFieldError()
  try {
    const result = await api.saveTemplate({
      id: current.id,
      display_name: current.displayName.trim(),
      content: current.content,
      canvas_width: Number(current.canvasWidth),
      expected_revision: current.revision,
      apply,
    })
    const savedTemplate = result.template ?? {}
    current.id = savedTemplate.id ?? current.id
    current.displayName = savedTemplate.display_name ?? current.displayName.trim()
    current.canvasWidth = Number(savedTemplate.canvas_width ?? current.canvasWidth)
    current.revision = savedTemplate.revision ?? current.revision
    current.builtIn = Boolean(savedTemplate.built_in ?? false)
    current.valid = savedTemplate.valid !== false
    state.activeId = normalizeTemplateId(result.active) ?? (apply ? current.id : state.activeId)
    state.effectiveActiveId = normalizeTemplateId(result.effective_active) ?? state.effectiveActiveId
    current.active = current.id === state.activeId
    markSaved(current)
    detailCache.set(current.id, {
      id: current.id,
      display_name: current.displayName,
      content: current.content,
      canvas_width: current.canvasWidth,
      built_in: current.builtIn,
      revision: current.revision,
      active: current.active,
      valid: current.valid,
    })
    state.templates = upsertTemplate(state.templates, toTemplateSummary(current, savedTemplate))
    updateActiveFlags()
    renderCurrent()
    showNotice(apply ? '模板已保存并应用。' : current.active ? '模板已保存，当前应用内容已更新。' : '模板已保存。')
  } catch (error) {
    showNotice(describeApiError(error, apply ? '保存并应用' : '保存'), 'error')
  } finally {
    setBusy(null)
  }
}

async function applyCurrent() {
  const current = state.current
  if (!current?.id || isDirty(current) || state.busy) return
  setBusy('apply')
  try {
    const result = await api.applyTemplate({ id: current.id, expected_revision: current.revision })
    state.activeId = normalizeTemplateId(result?.active) ?? current.id
    state.effectiveActiveId = normalizeTemplateId(result?.effective_active) ?? current.id
    updateActiveFlags()
    renderCurrent()
    showNotice('模板已应用。')
  } catch (error) {
    showNotice(describeApiError(error, '应用模板'), 'error')
  } finally {
    setBusy(null)
  }
}

async function discardDraft() {
  if (!state.current || state.current.id !== null || state.busy) return
  const confirmed = await confirmAction({
    title: '放弃未保存草稿？',
    message: '草稿尚未保存，放弃后内容将无法恢复。',
    confirmLabel: '放弃草稿',
    danger: true,
  })
  if (!confirmed) return

  previewVersion += 1
  state.current = null
  renderCurrent({ replaceEditor: true })
  setPreviewState('empty', '草稿已放弃', '正在返回已保存模板。')

  const nextId = state.activeId ?? state.effectiveActiveId ?? state.templates[0]?.id
  if (nextId) await selectTemplate(nextId, { skipGuard: true, force: true })
}

async function refreshTemplateIndex(preferredId) {
  try {
    const bootstrap = await api.listTemplates()
    state.templates = Array.isArray(bootstrap.templates) ? bootstrap.templates : state.templates
    state.activeId = normalizeTemplateId(bootstrap.active) ?? state.activeId
    state.effectiveActiveId = normalizeTemplateId(bootstrap.effective_active) ?? state.effectiveActiveId
    updateActiveFlags()
  } catch (error) {
    showNotice(`模板已删除，但列表刷新失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  }

  detailCache.delete(preferredId)
  state.current = null
  renderCurrent({ replaceEditor: true })
  if (preferredId) await selectTemplate(preferredId, { skipGuard: true, force: true })
  else renderTemplateList()
}

async function deleteCurrent() {
  const current = state.current
  if (!current || state.busy) return
  if (current.id === null) {
    await discardDraft()
    return
  }
  if (current.builtIn) {
    showNotice('内置模板不可删除，可复制后再编辑。', 'error')
    return
  }

  const deletingActiveTemplate = current.id === state.activeId
  const builtInTemplate = state.templates.find(
    (template) => template.id === 'built-in' && template.built_in,
  )
  if (deletingActiveTemplate && !builtInTemplate) {
    showNotice('无法删除当前应用模板：未找到 built-in 内置模板。', 'error')
    return
  }
  const discardingChanges = isDirty(current) ? '当前未保存修改也会被放弃。' : ''
  const confirmed = await confirmAction({
    title: deletingActiveTemplate ? '切换并删除当前模板？' : '删除当前模板？',
    message: deletingActiveTemplate
      ? `${discardingChanges}系统将先应用内置模板，再删除“${current.displayName}”。删除后无法恢复。`
      : `${discardingChanges}确定删除“${current.displayName}”？删除后无法恢复。`,
    confirmLabel: deletingActiveTemplate ? '切换并删除' : '删除模板',
    danger: true,
  })
  if (!confirmed) return

  const deletedId = current.id
  setBusy('delete')
  let switchedToBuiltIn = false
  try {
    if (deletingActiveTemplate) {
      try {
        const applyResult = await api.applyTemplate({ id: 'built-in', expected_revision: 0 })
        state.activeId = normalizeTemplateId(applyResult?.active) ?? 'built-in'
        state.effectiveActiveId = normalizeTemplateId(applyResult?.effective_active) ?? 'built-in'
        switchedToBuiltIn = true
        updateActiveFlags()
        renderCurrent()
      } catch (error) {
        showNotice(
          `切换到内置模板失败，当前模板未删除：${error instanceof Error ? error.message : String(error)}`,
          'error',
        )
        return
      }
    }

    try {
      await api.deleteTemplate({ id: deletedId, expected_revision: current.revision })
    } catch (error) {
      if (switchedToBuiltIn) {
        updateActiveFlags()
        renderCurrent()
        showNotice(
          `已切换到内置模板，但删除失败：${error instanceof Error ? error.message : String(error)}`,
          'error',
        )
        return
      }
      throw error
    }

    detailCache.delete(deletedId)
    state.templates = state.templates.filter((template) => template.id !== deletedId)
    const nextId = deletingActiveTemplate
      ? 'built-in'
      : state.activeId ?? state.effectiveActiveId ?? state.templates[0]?.id
    await refreshTemplateIndex(nextId)
    showNotice(deletingActiveTemplate ? '已切换到内置模板并删除当前模板。' : '模板已删除。')
  } catch (error) {
    showNotice(describeApiError(error, '删除模板'), 'error')
  } finally {
    setBusy(null)
  }
}

function showFieldError(message) {
  elements['field-error'].textContent = message
  elements['field-error'].classList.remove('is-hidden')
}

function clearFieldError() {
  elements['field-error'].textContent = ''
  elements['field-error'].classList.add('is-hidden')
}

function parsePreviewEvent() {
  try {
    const value = JSON.parse(editors.getEvent())
    elements['json-error'].textContent = ''
    elements['json-error'].classList.add('is-hidden')
    return { value, error: null }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    elements['json-error'].textContent = `JSON 解析错误：${message}`
    elements['json-error'].classList.remove('is-hidden')
    return { value: null, error }
  }
}

function schedulePreview({ immediate = false } = {}) {
  window.clearTimeout(previewTimer)
  const version = ++previewVersion
  const parsed = parsePreviewEvent()
  if (parsed.error) {
    setPreviewState('error', '预览数据格式有误', '修正 JSON 后将自动重新预览。')
    return
  }
  if (!state.current || !api) return

  previewTimer = window.setTimeout(() => runPreview(version, parsed.value), immediate ? 0 : 500)
}

async function runPreview(version, event) {
  const current = state.current
  if (!current || !api) return
  const width = Number(current.canvasWidth)
  if (!Number.isInteger(width) || width < CANVAS_MIN || width > CANVAS_MAX) {
    setPreviewState('error', '画布宽度无效', `请输入 ${CANVAS_MIN} 到 ${CANVAS_MAX} 之间的整数。`)
    return
  }

  previewBusyVersion = version
  renderControls()
  setPreviewState('loading', '正在渲染', '等待后端生成安全预览。')
  try {
    const result = await api.previewTemplate({ content: current.content, event, canvas_width: width })
    if (version !== previewVersion) return
    const resultWidth = Number(result.canvas_width ?? width)
    previewCanvasWidth = resultWidth
    elements['preview-frame'].srcdoc = result.html ?? ''
    elements['preview-width-label'].textContent = `${resultWidth}px`
    elements['preview-placeholder'].classList.add('is-hidden')
    elements['preview-canvas'].classList.remove('is-hidden')
    elements['preview-canvas'].setAttribute('aria-hidden', 'false')
    elements['preview-stage'].dataset.state = 'success'
    window.requestAnimationFrame(recalculatePreviewScale)
  } catch (error) {
    if (version !== previewVersion) return
    setPreviewState('error', '预览失败', describeApiError(error, '生成预览'))
  } finally {
    if (previewBusyVersion === version) {
      previewBusyVersion = null
      renderControls()
    }
  }
}

function setPreviewState(stateName, title, message) {
  elements['preview-stage'].dataset.state = stateName
  elements['preview-state-title'].textContent = title
  elements['preview-state-message'].textContent = message
  elements['preview-placeholder'].classList.remove('is-hidden')
  elements['preview-canvas'].classList.add('is-hidden')
  elements['preview-canvas'].setAttribute('aria-hidden', 'true')
  if (stateName !== 'success') elements['preview-width-label'].textContent = '—'
}

function recalculatePreviewScale() {
  if (!previewCanvasWidth || elements['preview-canvas'].classList.contains('is-hidden')) return

  const stageWidth = elements['preview-stage'].clientWidth
  const stageHeight = elements['preview-stage'].clientHeight
  if (!stageWidth || !stageHeight) return

  const horizontalInset = 24
  const availableWidth = Math.max(1, stageWidth - horizontalInset)
  const scale = Math.min(1, availableWidth / previewCanvasWidth)
  const visualWidth = previewCanvasWidth * scale
  const logicalHeight = Math.max(320, Math.floor(stageHeight / scale))
  const left = Math.max(0, (stageWidth - visualWidth) / 2)

  elements['preview-canvas'].style.width = `${previewCanvasWidth}px`
  elements['preview-canvas'].style.height = `${logicalHeight}px`
  elements['preview-canvas'].style.left = `${left}px`
  elements['preview-canvas'].style.transform = `scale(${scale})`
  elements['preview-canvas'].dataset.scale = scale.toFixed(4)
}

function observePreviewSize() {
  const resizePreview = () => window.requestAnimationFrame(recalculatePreviewScale)
  if ('ResizeObserver' in window) {
    previewResizeObserver = new ResizeObserver(resizePreview)
    previewResizeObserver.observe(elements['preview-stage'])
  } else {
    window.addEventListener('resize', resizePreview)
  }
}

function resetPreviewEvent() {
  editors.setEvent(state.defaultEventText)
  schedulePreview({ immediate: true })
  showNotice('预览数据已恢复默认。')
}

function bindEvents() {
  elements['retry-button'].addEventListener('click', initialize)
  elements['copy-base-url-button'].addEventListener('click', copyBaseUrl)
  elements['retry-base-url-button'].addEventListener('click', loadBaseUrl)
  elements['new-template-button'].addEventListener('click', createNewTemplate)
  elements['copy-template-button'].addEventListener('click', copyCurrentTemplate)
  elements['delete-template-button'].addEventListener('click', deleteCurrent)
  elements['save-button'].addEventListener('click', () => saveCurrent(false))
  elements['apply-button'].addEventListener('click', applyCurrent)
  elements['save-apply-button'].addEventListener('click', () => saveCurrent(true))
  elements['refresh-preview-button'].addEventListener('click', () => schedulePreview({ immediate: true }))
  elements['reset-event-button'].addEventListener('click', resetPreviewEvent)
  elements['preview-data-panel'].addEventListener('toggle', () => {
    if (elements['preview-data-panel'].open) window.setTimeout(() => editors.layoutEvent(), 0)
  })

  elements['template-name'].addEventListener('input', (event) => {
    if (!state.current) return
    state.current.displayName = event.target.value
    clearFieldError()
    renderControls()
    renderTemplateList()
  })
  elements['canvas-width'].addEventListener('input', (event) => {
    if (!state.current) return
    state.current.canvasWidth = event.target.value
    clearFieldError()
    renderControls()
    schedulePreview()
  })

  window.addEventListener('beforeunload', (event) => {
    if (!isDirty(state.current)) return
    event.preventDefault()
    event.returnValue = ''
  })
}

function updateWorkerStatus(diagnostics) {
  const chip = elements['worker-status']
  chip.dataset.state = diagnostics.status
  const labels = {
    loading: '编辑器初始化中',
    checking: '编辑器就绪 · Worker 检查中',
    ready: '编辑器与 Worker 已就绪',
    warning: '编辑器就绪 · Worker 诊断异常',
  }
  elements['worker-status-text'].textContent = labels[diagnostics.status] ?? '编辑器状态未知'
  chip.title = diagnostics.error ?? ''
}

async function initialize() {
  if (state.busy) return
  showGate({
    title: '正在载入模板',
    message: '正在连接 AstrBot 插件页面并读取模板列表。',
    loading: true,
  })
  setBusy('bootstrap')

  try {
    const connection = await connectBridge()
    api = createTemplateApi(connection.bridge)
    setTheme(connection.context)
    const bootstrap = await api.listTemplates()
    state.templates = Array.isArray(bootstrap.templates) ? bootstrap.templates : []
    state.activeId = normalizeTemplateId(bootstrap.active)
    state.effectiveActiveId = normalizeTemplateId(bootstrap.effective_active)
    state.sampleEvent = bootstrap.sample_event ?? {}
    state.defaultEventText = prettyJson(state.sampleEvent)
    editors.setEvent(state.defaultEventText)
    updateActiveFlags()
    state.initialized = true
    showWorkspace()
    void loadBaseUrl()
    renderTemplateList()

    const initialId = state.activeId ?? state.effectiveActiveId ?? state.templates[0]?.id
    if (initialId) {
      await selectTemplate(initialId, { skipGuard: true, force: true })
    } else {
      state.current = null
      renderCurrent()
      setPreviewState('empty', '没有可预览的模板', '请检查后端是否已初始化内置模板。')
    }
  } catch (error) {
    api = null
    state.initialized = false
    if (error instanceof BridgeUnavailableError) {
      showGate({
        title: '无法打开模板管理页',
        message: '必须从AstrBot插件详情页打开',
        retry: false,
      })
    } else {
      showGate({
        title: '模板载入失败',
        message: describeApiError(error, '载入模板列表'),
        retry: true,
      })
    }
  } finally {
    setBusy(null)
  }
}

function start() {
  editors = createMonacoEditors({
    templateElement: elements['template-editor'],
    eventElement: elements['event-editor'],
    onTemplateChange(content) {
      if (!state.current) return
      state.current.content = content
      renderControls()
      schedulePreview()
    },
    onEventChange() {
      schedulePreview()
    },
  })
  onWorkerDiagnostics(updateWorkerStatus)
  bindEvents()
  observePreviewSize()
  setTheme()
  initialize()
  runWorkerDiagnostics()
}

window.addEventListener('error', (event) => {
  if (state.initialized) showNotice(`页面错误：${event.message}`, 'error')
})
window.addEventListener('unhandledrejection', (event) => {
  if (state.initialized) showNotice(`未处理的请求错误：${String(event.reason)}`, 'error')
})

start()
