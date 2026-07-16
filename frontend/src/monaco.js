import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import 'monaco-editor/esm/vs/basic-languages/css/css.contribution'
import 'monaco-editor/esm/vs/basic-languages/html/html.contribution'
import 'monaco-editor/esm/vs/language/css/monaco.contribution'
import 'monaco-editor/esm/vs/language/json/monaco.contribution'
import 'monaco-editor/esm/vs/language/html/monaco.contribution'
import 'monaco-editor/esm/vs/editor/contrib/format/browser/formatActions'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker&inline'
import HtmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker&inline'
import CssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker&inline'
import JsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker&inline'

const phase0Diagnostics = {
  status: 'loading',
  editorLoaded: false,
  workerRequests: [],
  workerMessages: { editor: 0, html: 0, css: 0, json: 0 },
  workerErrors: [],
  workersVerified: { html: false, css: false, json: false },
  workerDiagnosticsCount: { html: 0, css: 0, json: 0 },
  externalResources: [],
  error: null,
}

window.__WHN_MONACO_PHASE0__ = phase0Diagnostics

let diagnosticsListener = () => {}

function emitDiagnostics() {
  diagnosticsListener({ ...phase0Diagnostics })
}

function normalizeWorkerLabel(label) {
  if (label === 'html' || label === 'handlebars' || label === 'razor') return 'html'
  if (label === 'css' || label === 'scss' || label === 'less') return 'css'
  if (label === 'json') return 'json'
  return 'editor'
}

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    const normalizedLabel = normalizeWorkerLabel(label)
    phase0Diagnostics.workerRequests.push(label)

    const workers = {
      editor: EditorWorker,
      html: HtmlWorker,
      css: CssWorker,
      json: JsonWorker,
    }
    const WorkerConstructor = workers[normalizedLabel]
    const worker = new WorkerConstructor()

    worker.addEventListener('message', () => {
      phase0Diagnostics.workerMessages[normalizedLabel] += 1
      if (normalizedLabel !== 'editor') {
        phase0Diagnostics.workersVerified[normalizedLabel] = true
      }
      emitDiagnostics()
    })
    worker.addEventListener('error', (event) => {
      phase0Diagnostics.workerErrors.push({
        label: normalizedLabel,
        message: event.message || 'Unknown worker error',
      })
      emitDiagnostics()
    })
    emitDiagnostics()
    return worker
  },
}

function editorOptions(ariaLabel) {
  return {
    automaticLayout: true,
    ariaLabel,
    minimap: { enabled: false },
    fontFamily: "'SFMono-Regular', 'Cascadia Code', 'Roboto Mono', monospace",
    fontSize: 13,
    lineHeight: 21,
    padding: { top: 14, bottom: 14 },
    renderLineHighlight: 'line',
    roundedSelection: false,
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    tabSize: 2,
    wordWrap: 'on',
  }
}

export function createMonacoEditors({ templateElement, eventElement, onTemplateChange, onEventChange }) {
  let suppressTemplateChange = false
  let suppressEventChange = false

  const templateModel = monaco.editor.createModel(
    '',
    'html',
    monaco.Uri.parse('inmemory://webhook-notifier/template.html'),
  )
  const eventModel = monaco.editor.createModel(
    '{}',
    'json',
    monaco.Uri.parse('inmemory://webhook-notifier/preview-event.json'),
  )

  const templateEditor = monaco.editor.create(templateElement, {
    ...editorOptions('HTML 和 Jinja2 模板编辑器'),
    model: templateModel,
  })
  const eventEditor = monaco.editor.create(eventElement, {
    ...editorOptions('预览数据 JSON 编辑器'),
    model: eventModel,
    fontSize: 12,
    lineHeight: 19,
  })

  templateEditor.onDidChangeModelContent(() => {
    if (!suppressTemplateChange) onTemplateChange(templateEditor.getValue())
  })
  eventEditor.onDidChangeModelContent(() => {
    if (!suppressEventChange) onEventChange(eventEditor.getValue())
  })

  phase0Diagnostics.editorLoaded = true
  phase0Diagnostics.status = 'checking'
  emitDiagnostics()

  return {
    setTemplate(value, readOnly = false) {
      suppressTemplateChange = true
      templateEditor.setValue(value ?? '')
      suppressTemplateChange = false
      templateEditor.updateOptions({ readOnly })
    },
    setTemplateReadOnly(readOnly) {
      templateEditor.updateOptions({ readOnly })
    },
    getTemplate() {
      return templateEditor.getValue()
    },
    setEvent(value) {
      suppressEventChange = true
      eventEditor.setValue(value ?? '{}')
      suppressEventChange = false
    },
    getEvent() {
      return eventEditor.getValue()
    },
    layoutEvent() {
      eventEditor.layout()
    },
    focusTemplate() {
      templateEditor.focus()
    },
  }
}

export function setMonacoTheme(theme) {
  monaco.editor.setTheme(theme === 'dark' ? 'vs-dark' : 'vs')
}

export function onWorkerDiagnostics(listener) {
  diagnosticsListener = listener
  emitDiagnostics()
}

export async function runWorkerDiagnostics(timeoutMs = 8000) {
  const definitions = [
    ['html', '<!doctype html><html><body><main>Worker check</main></body></html>', 'check.html'],
    ['css', ':root { color: #1f2937; }', 'check.css'],
    ['json', '{"worker":"ready"}', 'check.json'],
  ]
  const models = []
  let diagnosticEditor = null
  let diagnosticHost = null
  let diagnosticWarning = null

  try {
    models.push(
      ...definitions.map(([language, content, filename]) =>
        monaco.editor.createModel(
          content,
          language,
          monaco.Uri.parse(`inmemory://webhook-notifier/diagnostics/${filename}`),
        ),
      ),
    )

    diagnosticHost = document.createElement('div')
    diagnosticHost.setAttribute('aria-hidden', 'true')
    diagnosticHost.style.position = 'fixed'
    diagnosticHost.style.left = '-10000px'
    diagnosticHost.style.top = '-10000px'
    diagnosticHost.style.width = '1px'
    diagnosticHost.style.height = '1px'
    diagnosticHost.style.opacity = '0'
    diagnosticHost.style.overflow = 'hidden'
    diagnosticHost.style.pointerEvents = 'none'
    document.body.append(diagnosticHost)

    diagnosticEditor = monaco.editor.create(diagnosticHost, {
      model: models[0],
      automaticLayout: false,
      ariaLabel: 'HTML worker diagnostics',
      minimap: { enabled: false },
    })

    const formatAction = diagnosticEditor.getAction('editor.action.formatDocument')
    const formatSupportStartedAt = performance.now()
    const formatSupportTimeoutMs = 1500
    while (formatAction && !formatAction.isSupported()) {
      if (performance.now() - formatSupportStartedAt >= formatSupportTimeoutMs) break
      await new Promise((resolve) => window.setTimeout(resolve, 25))
    }

    if (!formatAction) {
      diagnosticWarning = 'The HTML format action is unavailable for worker diagnostics.'
    } else if (!formatAction.isSupported()) {
      diagnosticWarning = 'The HTML format action was not supported before its diagnostic timeout.'
    } else {
      try {
        await formatAction.run()
      } catch (error) {
        diagnosticWarning = error instanceof Error ? error.message : String(error)
      }
    }

    const startedAt = performance.now()
    while (!Object.values(phase0Diagnostics.workersVerified).every(Boolean)) {
      if (performance.now() - startedAt >= timeoutMs) {
        diagnosticWarning =
          diagnosticWarning ?? 'Language workers did not all answer before the diagnostic timeout.'
        break
      }
      await new Promise((resolve) => window.setTimeout(resolve, 50))
    }
    models.forEach((model, index) => {
      const language = definitions[index][0]
      phase0Diagnostics.workerDiagnosticsCount[language] = monaco.editor.getModelMarkers({
        resource: model.uri,
      }).length
    })
    phase0Diagnostics.status = diagnosticWarning ? 'warning' : 'ready'
    phase0Diagnostics.error = diagnosticWarning
  } catch (error) {
    phase0Diagnostics.status = 'warning'
    phase0Diagnostics.error = error instanceof Error ? error.message : String(error)
  } finally {
    diagnosticEditor?.dispose()
    models.forEach((model) => model.dispose())
    diagnosticHost?.remove()
    emitDiagnostics()
  }
}
