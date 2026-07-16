export function createConfirmAction({ overlay, dialog, title, message, cancelButton, confirmButton }) {
  let pendingResolve = null
  let previousFocus = null

  function focusableElements() {
    return [cancelButton, confirmButton].filter((element) => !element.disabled)
  }

  function close(result) {
    if (!pendingResolve) return
    const resolve = pendingResolve
    pendingResolve = null
    overlay.hidden = true
    document.body.classList.remove('has-confirm-overlay')
    resolve(result)

    const focusTarget = previousFocus
    previousFocus = null
    if (focusTarget instanceof HTMLElement && focusTarget.isConnected) {
      window.requestAnimationFrame(() => focusTarget.focus())
    }
  }

  function handleKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault()
      close(false)
      return
    }
    if (event.key !== 'Tab') return

    const focusable = focusableElements()
    if (!focusable.length) {
      event.preventDefault()
      dialog.focus()
      return
    }

    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  cancelButton.addEventListener('click', () => close(false))
  confirmButton.addEventListener('click', () => close(true))
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) close(false)
  })
  dialog.addEventListener('keydown', handleKeydown)

  return function confirmAction({
    title: nextTitle,
    message: nextMessage,
    confirmLabel = '确认',
    danger = false,
  }) {
    if (pendingResolve) close(false)

    return new Promise((resolve) => {
      pendingResolve = resolve
      previousFocus = document.activeElement
      title.textContent = nextTitle
      message.textContent = nextMessage
      confirmButton.textContent = confirmLabel
      confirmButton.classList.toggle('button-danger', danger)
      confirmButton.classList.toggle('button-primary', !danger)
      overlay.hidden = false
      document.body.classList.add('has-confirm-overlay')
      window.requestAnimationFrame(() => cancelButton.focus())
    })
  }
}
