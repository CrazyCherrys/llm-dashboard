'use strict';
// Shared dialog behavior keeps dismissal, scroll locking and focus consistent.
window.llmDashboardUI = (() => {
  const dialogs = new WeakMap();
  let scrollState;
  const captureFocus = (root = document) => {
    const element = document.activeElement;
    if (!element || !root.contains(element) || element === document.body) return null;
    let selector = element.id ? `#${CSS.escape(element.id)}` : '';
    if (!selector) {
      const scope = element.closest('[data-model], [data-channel]');
      const prefix = scope
        ? `[${scope.hasAttribute('data-model') ? 'data-model' : 'data-channel'}="${scope.dataset.model || scope.dataset.channel}"] `
        : '';
      const attributes = [...element.attributes].filter((a) => a.name.startsWith('data-'));
      if (attributes.length) {
        selector = prefix + element.tagName.toLowerCase();
        if (element.classList.length) selector += `.${CSS.escape(element.classList[0])}`;
        selector += attributes.map((a) => `[${a.name}="${CSS.escape(a.value)}"]`).join('');
      } else if (element.tagName === 'SUMMARY') {
        selector = prefix + 'details > summary';
      }
    }
    return { element, selector };
  };
  const resolveFocus = (ref) =>
    ref?.element?.isConnected ? ref.element : ref?.selector ? document.querySelector(ref.selector) : null;
  function restoreFocus(ref, fallback) {
    const element = resolveFocus(ref) || fallback;
    const topDialog = [...document.querySelectorAll('dialog[open]')].at(-1);
    if (element && !element.disabled && element.getClientRects().length && (!topDialog || topDialog.contains(element)))
      element.focus({ preventScroll: true });
  }
  function nodeKey(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.id) return `id:${node.id}`;
    const attributes = [...node.attributes].filter((attribute) => attribute.name.startsWith('data-'));
    return attributes.length ? `${node.tagName}.${node.classList[0] || ''}:${attributes.map((a) => `${a.name}=${a.value}`).join('|')}` : '';
  }
  function sameNodeType(left, right) {
    return left.nodeType === right.nodeType && (left.nodeType !== Node.ELEMENT_NODE ||
      (left.tagName === right.tagName && left.classList[0] === right.classList[0]));
  }
  // Keep matching DOM nodes so polling preserves focus, selection and loaded images.
  function patchElement(target, source) {
    const retained = (name) => (name === 'open' && target.tagName === 'DETAILS') ||
      (name === 'aria-expanded' && target.classList.contains('thumbnail'));
    for (const attribute of [...target.attributes]) {
      if (!source.hasAttribute(attribute.name) && !retained(attribute.name)) target.removeAttribute(attribute.name);
    }
    for (const attribute of source.attributes) {
      if (!retained(attribute.name) && target.getAttribute(attribute.name) !== attribute.value)
        target.setAttribute(attribute.name, attribute.value);
    }
    patchChildren(target, source);
  }
  function patchChildren(target, source) {
    const old = [...target.childNodes];
    const keyed = new Map(old.map((node) => [nodeKey(node), node]).filter(([key]) => key));
    const used = new Set();
    [...source.childNodes].forEach((fresh, index) => {
      const key = nodeKey(fresh);
      let match = key ? keyed.get(key) : old.find((node) => !used.has(node) && !nodeKey(node) && sameNodeType(node, fresh));
      if (match && !sameNodeType(match, fresh)) match = null;
      if (!match) match = fresh.cloneNode(true);
      else if (match.nodeType === Node.ELEMENT_NODE) patchElement(match, fresh);
      else if (match.nodeValue !== fresh.nodeValue) match.nodeValue = fresh.nodeValue;
      used.add(match);
      if (target.childNodes[index] !== match) target.insertBefore(match, target.childNodes[index] || null);
    });
    old.forEach((node) => { if (!used.has(node)) node.remove(); });
  }
  function syncScrollLock() {
    if (document.querySelector('dialog[open]')) {
      if (scrollState) return;
      scrollState = {
        x: scrollX,
        y: scrollY,
        top: document.body.style.top,
        left: document.body.style.left,
        padding: document.body.style.paddingRight,
      };
      const gutter = innerWidth - document.documentElement.clientWidth;
      document.body.style.top = `${-scrollState.y}px`;
      document.body.style.left = `${-scrollState.x}px`;
      if (gutter) document.body.style.paddingRight = `${parseFloat(getComputedStyle(document.body).paddingRight) + gutter}px`;
      document.documentElement.classList.add('modal-open');
      document.body.classList.add('modal-open');
    } else if (scrollState) {
      const previous = scrollState;
      scrollState = null;
      document.documentElement.classList.remove('modal-open');
      document.body.classList.remove('modal-open');
      document.body.style.top = previous.top;
      document.body.style.left = previous.left;
      document.body.style.paddingRight = previous.padding;
      window.scrollTo({ left: previous.x, top: previous.y, behavior: 'instant' });
    }
  }
  function bindDialog(dialog, options = {}) {
    const state = { options, closing: false, returnFocus: null };
    dialogs.set(dialog, state);
    let backdropStart = false;
    const outside = (event) => {
      const rect = dialog.getBoundingClientRect();
      return event.target === dialog &&
        (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
    };
    dialog.addEventListener('pointerdown', (event) => { backdropStart = event.button === 0 && outside(event); });
    dialog.addEventListener('pointercancel', () => { backdropStart = false; });
    dialog.addEventListener('click', (event) => {
      const dismiss = backdropStart && outside(event);
      backdropStart = false;
      if (dismiss) requestClose(dialog);
    });
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      requestClose(dialog);
    });
    dialog.addEventListener('close', () => {
      if (dialog.open) return;
      state.closing = false;
      syncScrollLock();
      // Native dialog closing may already restore focus before this queued event.
      // Respect a user's newer focus choice outside the dialog.
      if (document.activeElement === document.body || dialog.contains(document.activeElement))
        restoreFocus(state.returnFocus, state.options.fallbackFocus);
      state.options.onClose?.();
    });
  }
  function openDialog(dialog, initialFocus) {
    const state = dialogs.get(dialog);
    if (!dialog.open) {
      if (state) state.returnFocus = captureFocus();
      document.dispatchEvent(new Event('dialogopening'));
      dialog.showModal();
      syncScrollLock();
    }
    initialFocus?.focus({ preventScroll: true });
  }
  async function requestClose(dialog) {
    const state = dialogs.get(dialog);
    if (!dialog.open || state?.closing) return;
    if (state) state.closing = true;
    try {
      if (!state?.options.beforeClose || await state.options.beforeClose()) dialog.close();
    } finally {
      if (state) state.closing = false;
    }
  }
  function confirmDiscard() {
    return new Promise((resolve) => {
      const dialog = document.createElement('dialog');
      dialog.className = 'confirm-dialog';
      dialog.setAttribute('aria-labelledby', 'discard-title');
      dialog.setAttribute('aria-describedby', 'discard-description');
      dialog.innerHTML = '<h2 id="discard-title">放弃未保存的修改？</h2><p id="discard-description">关闭后，本次填写的内容不会保存。</p><div class="dialog-actions"><button class="button danger" type="button" data-discard>放弃修改</button><button class="button primary" type="button" data-continue autofocus>继续编辑</button></div>';
      document.body.append(dialog);
      let discard = false;
      bindDialog(dialog, { onClose: () => { dialog.remove(); resolve(discard); } });
      dialog.querySelector('[data-discard]').addEventListener('click', () => { discard = true; dialog.close(); });
      dialog.querySelector('[data-continue]').addEventListener('click', () => dialog.close());
      openDialog(dialog);
    });
  }
  return { bindDialog, openDialog, requestClose, confirmDiscard, captureFocus, restoreFocus, resolveFocus, patchElement, patchChildren };
})();
