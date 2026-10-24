// Presentation-only reading cards. Recognition and proof status belong to app.js.
(() => {
  'use strict';
  const esc = value => String(value).replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const token = value => /^[a-z][a-z0-9-]*$/.test(value) ? value : 'context';
  // A Lean keyword chooses a visual kind and a human label, never a proof status.
  const declarations = Object.freeze(Object.fromEntries(Object.entries({
    def: ['definition', 'Definition'], abbrev: ['definition', 'Definition'],
    opaque: ['definition', 'Opaque definition'],
    structure: ['type', 'Structure'], inductive: ['type', 'Inductive type'],
    class: ['type', 'Class'], 'class inductive': ['type', 'Inductive class'],
    instance: ['instance', 'Instance'], axiom: ['assumption', 'Assumption'],
    example: ['example', 'Example'], theorem: ['proposition', 'Theorem'], lemma: ['proposition', 'Lemma'],
  }).map(([keyword, [kind, label]]) => [keyword, Object.freeze({kind, label})])));

  // Text slots are escaped here. HTML slots must come from trusted renderers
  // which escape notebook text; raw Lean/docstrings must never enter HTML slots.
  function header({label, name = '', signature = '', descriptionHTML = '', locationHTML = '', badgesHTML = '', classes = []}) {
    return `<h3 class="card-header def-head ${classes.map(token).join(' ')}"><span class="def-kind">${esc(label)}</span> ${name ? `<dfn><code>${esc(name)}</code></dfn>` : ''} ${signature ? `<span class="card-signature"><code>${esc(signature)}</code></span>` : ''} ${badgesHTML} ${locationHTML}${descriptionHTML ? `<span class="card-description def-body">${descriptionHTML}</span>` : ''}</h3>`;
  }
  function render({kind = 'context', label, name = '', signature = '', descriptionHTML = '', classes = [], locationHTML = '', bodyHTML = '', sourceHTML = ''}) {
    const className = ['cell', 'reading-card', `card-${token(kind)}`, ...classes.map(token)].join(' ');
    return `<section class="${className}">
      ${header({label, name, signature, descriptionHTML, locationHTML})}
      <div class="card-body">${bodyHTML}</div>
      ${sourceHTML ? `<div class="card-source">${sourceHTML}</div>` : ''}
    </section>`;
  }
  window.glitnirCards = Object.freeze({declarations, header, render});
})();
