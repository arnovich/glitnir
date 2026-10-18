// `glitnirHost` and `glitnir-*` events are the installed renderer’s shared API.
// The webview has no network access. Only the extension can contact its server.
(() => {
  'use strict';
  const vscode = acquireVsCodeApi();
  const pending = new Map();
  let sequence = 0;
  let dirty = false;
  const images = /^image\/(png|jpeg|gif|webp|svg\+xml)$/;
  // Posts that only stop work never depend on the editor's text. The
  // extension host applies the same list (STOP_PATHS in backend.ts).
  const STOP_PATHS = ['/api/proof/cancel', '/api/decide/stop', '/api/view/cancel'];

  function request(path, options = {}, type = 'request') {
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error('Glitnir did not answer')); }, 150000);
      pending.set(id, {resolve, timeout});
      vscode.postMessage({type, id, path, method: options.method || 'GET', body: options.body});
    });
  }
  window.addEventListener('message', event => {
    const message = event.data;
    if (message.type === 'response') {
      const item = pending.get(message.id);
      if (item) { clearTimeout(item.timeout); pending.delete(message.id); item.resolve(message); }
    } else if (message.type === 'revealCell') {
      window.glitnirHost.pendingReveal = message;
      if (window.glitnirHost.openSelectedSource) document.dispatchEvent(new CustomEvent('glitnir-reveal-cell', {detail: message}));
    } else if (message.type === 'sourceChanged') {
      document.dispatchEvent(new CustomEvent('glitnir-source-changed'));
    } else if (message.type === 'environmentChanged') {
      document.dispatchEvent(new CustomEvent('glitnir-environment-changed'));
    } else if (message.type === 'editorState') {
      dirty = message.dirty;
      document.body.classList.toggle('unsaved-lean', dirty);
      const state = document.getElementById('editor-state');
      state.hidden = !dirty;
      state.textContent = dirty ? 'Unsaved Lean changes — save or revert before checking. Preview shows the saved source.' : '';
    }
  });
  window.glitnirHost = {
    async gitHistory() {
      const reply = await request('', {}, 'gitHistory');
      if (reply.status !== 200) throw new Error(reply.json?.error || 'Git history is unavailable.');
      return reply.json;
    },
    async gitDiff(sha) {
      const reply = await request('', {body: sha}, 'gitDiff');
      if (reply.status !== 200) throw new Error(reply.json?.error || 'Git diff is unavailable.');
      return reply.json.diff;
    },
    openSource(cell) { vscode.postMessage({type: 'source', ...cell}); },
    async fetch(path, options = {}) {
      if (dirty && options.method === 'POST' && !STOP_PATHS.includes(path)) {
        return new Response(JSON.stringify({error: 'Save or revert unsaved Lean files first.'}), {status: 409});
      }
      const reply = await request(path, options);
      return new Response(JSON.stringify(reply.json), {status: reply.status, headers: {'Content-Type': 'application/json'}});
    },
    async copyText(text) {
      const reply = await request('', {body: text}, 'clipboard');
      if (reply.status !== 200) throw new Error('Could not copy the command.');
    },
    async saveHTML(html) {
      const reply = await request('', {body: html}, 'export');
      if (reply.status !== 200) throw new Error(reply.json?.error || 'Could not save HTML.');
    },
    // An image from the notebook's assets/ folder, as a data URL for an <img>.
    async asset(path) {
      const reply = await request(path);
      if (reply.status !== 200 || !reply.data || !images.test(reply.contentType || '')) {
        throw Object.assign(new Error(reply.json?.error || 'the image could not be read'), {status: reply.status === 200 ? 415 : reply.status});
      }
      return `data:${reply.contentType};base64,${reply.data}`;
    },
  };

  document.addEventListener('click', event => {
    const target = event.target.closest('a,button');
    if (!target) return;
    if (target.dataset.act === 'accept') {
      // Accepting writes the notebook, which only the editor may do: the
      // preview names the decision and the editor fetches its checked block.
      event.preventDefault(); event.stopImmediatePropagation();
      vscode.postMessage({type: 'acceptDecision', key: target.dataset.key});
      return;
    }
    if (target.tagName === 'A') {
      const href = target.getAttribute('href') || '';
      if (href.startsWith('/?reference=')) {
        event.preventDefault();
        // A project notebook link is read in this panel by the renderer,
        // through the request bridge; any other opens the sibling's own preview.
        if (target.dataset.act === 'open-notebook') return;
        event.stopImmediatePropagation();
        vscode.postMessage({type: 'reference', module: new URLSearchParams(href.slice(2)).get('reference')});
      } else if (!href.startsWith('#')) {
        // Source links are text-only in this first preview; never invoke commands.
        event.preventDefault(); event.stopImmediatePropagation();
      }
    }
  }, true);
  vscode.postMessage({type: 'ready'});
})();
