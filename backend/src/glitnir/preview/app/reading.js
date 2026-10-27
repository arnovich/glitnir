// The workbench's reading view: Lean read as a textbook.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, DECLARATION, HOST, UI, checking, claim, decisionHTML, directiveNotes, esc, evidenceHTML, icon,
    inlineHTML, leanCellHTML, leanHTML, linkedPath, markdown, needsDecider, needsProvider, nonClaimsHTML,
    projectCardsHTML, projectClaimHTML, projectEvidence, refresh, showNotebookAt, sourceDirectivesHTML,
    statusOf, summariesOf, title, toneOf, useHTML, viewHTML, withheldBannerHTML} = workbench;
  // Defined by a later file and bound when it loads: nothing may call one while the page is still loading.
  const refutationHTML = (...a) => workbench.refutationHTML(...a);
  const render = (...a) => workbench.render(...a);
  const showSourceLines = (...a) => workbench.showSourceLines(...a);
  // ----- Lean read as a textbook ----------------------------------------------
  // Presentation only: recognized declarations get mathematical cards and an
  // exact source fold. Unknown syntax gets an explicit card, never disappears.
  const TRUST = /\b(sorry|admit|axiom|opaque|unsafe|implemented_by|extern|native_decide|set_option|variable|universe)\b|\bdebug\./;

  // Column-0 lines that continue the command above them.
  const CONTINUES = /^(\||deriving\b|where\b|termination_by\b|decreasing_by\b|with\b)/;
  // Column-0 lines that lead into the command below them.
  const LEADS = /^(@\[.*\]|.*\bin)\s*$/;

  // True when every block comment opens at the start of a line, closes at the
  // end of one and does not nest, so line shapes can find the commands.
  function plainComments(entries) {
    let inside = false;
    for (const [, text] of entries) {
      const code = inside || text.startsWith('/-') ? text : text.replace(/--.*$/, '');
      const opens = (code.match(/\/-/g) || []).length;
      const closes = (code.match(/-\//g) || []).length;
      if (inside) {
        if (opens || closes > 1 || (closes && !code.trimEnd().endsWith('-/'))) return false;
        inside = !closes;
      } else if (opens) {
        if (opens > 1 || !code.startsWith('/-') || closes > 1 || (closes && !code.trimEnd().endsWith('-/'))) return false;
        inside = !closes;
      } else if (closes) return false;
    }
    return !inside;
  }

  // Split [line, text] entries into top-level commands: {doc, notes, body}.
  // A command starts at a column-0 line; its docstring, and the comments and
  // leading lines above it, lead into it. Comments that turn out to sit inside
  // a body, because indented lines follow, stay with it.
  function commands(entries) {
    const out = [];
    let doc = [];
    let notes = [];
    let held = [];
    let current = null;
    let comment = null;
    const close = () => { if (current) out.push(current); current = null; notes.push(...held); held = []; };
    for (const entry of entries) {
      const text = entry[1];
      if (comment) { comment.push(entry); if (text.includes('-/')) comment = null; continue; }
      if (text.startsWith('/--')) { close(); notes.push(...doc); doc = [entry]; if (!text.slice(3).includes('-/')) comment = doc; continue; }
      if (text.startsWith('/-')) { close(); notes.push(entry); if (!text.slice(2).includes('-/')) comment = notes; continue; }
      if (current && (!text.trim() || text.startsWith('--'))) { held.push(entry); continue; }
      if (current && (/^\s/.test(text) || CONTINUES.test(text))) { current.body.push(...held, entry); held = []; continue; }
      close();
      if (!text.trim() || text.startsWith('--') || LEADS.test(text)) { notes.push(entry); continue; }
      current = {doc, notes, body: [entry]};
      doc = [];
      notes = [];
    }
    close();
    if (doc.length || notes.length) out.push({doc: [], notes: [...notes, ...doc].sort((a, b) => a[0] - b[0]), body: []});
    return out;
  }

  // Entries without the blank lines at either end.
  function trimmed(entries) {
    const at = entries.findIndex(e => e[1].trim());
    if (at < 0) return [];
    let to = entries.length;
    while (!entries[to - 1][1].trim()) to--;
    return entries.slice(at, to);
  }
  function lineLinkHTML(start, end) {
    const cell = UI.data.cells.find(c => c.start <= start && end <= c.end);
    const edit = HOST && cell && !linkedPath() ? `<button type="button" class="source-edit" data-act="source" data-index="${cell.index}" data-start="${start}" data-end="${end}" data-sha="${esc(UI.data.sha256)}" title="Edit in editor" aria-label="Edit in editor">${icon('edit')}</button>` : '';
    return `<span class="source-controls"><a class="lines source-link" href="#source-L${start}" data-act="source-lines" data-sha="${esc(UI.data.sha256)}" data-start="${start}" data-end="${end}" aria-label="Show lines ${start}–${end} in Source" title="Show in Source">L${start}–${end}</a>${edit}</span>`;
  }
  const leanOf = entries => entries.map(e => e[1]).join('\n');
  // A cell's [line, text] entries; a source cell's text ends with a newline.
  const cellEntries = cell => (cell.text.endsWith('\n') ? cell.text.slice(0, -1) : cell.text).split('\n').map((text, i) => [cell.start + i, text]);
  // An odd number of quotes may open a multiline string, which can imitate
  // top-level commands in the line reader.
  const unevenQuotes = line => (line.replace(/\\./g, '').replace(/--.*$/, '').match(/"/g) || []).length % 2 === 1;
  // Scaffolding such as imports, namespaces and directives: exact and quiet.
  const quietSourceHTML = entries => `<div class="cell source-context"><pre>${leanHTML(leanOf(entries))}</pre>${lineLinkHTML(entries[0][0], entries[entries.length - 1][0])}</div>`;

  // A project source cell read as the reading view reads Lean: cards for
  // declarations, prose for comments, quiet lines for scaffolding. A listed
  // directive at column 0 is replaced by its card, at its line; one no
  // provider serves keeps its exact line with a note. Returns the HTML and
  // the directive lines placed. Source the line reader cannot read safely is
  // shown whole, and so is a cell holding a `/-!` comment: the server keeps
  // prose in source cells only when it could not confirm comment boundaries.
  function projectSourceHTML(cell, scopes = new Map(), staticPage = false) {
    const entries = cellEntries(cell);
    const placed = new Set();
    if (entries.some(([, line]) => unevenQuotes(line) || line.startsWith('/-!'))) {
      const shown = trimmed(entries);
      const html = shown.length ? `<section class="cell">${lineLinkHTML(shown[0][0], shown[shown.length - 1][0])}<pre class="lean-source">${leanHTML(leanOf(shown))}</pre></section>` : '';
      return {html, placed};
    }
    const notes = new Map(directiveNotes(cell));
    const directives = new Map();
    const listed = (start, end, card) => {
      const lines = entries.filter(([n]) => n >= start && n <= end);
      if (lines.length && lines.every(([, text]) => text.startsWith('--'))) directives.set(start, {lines, card});
    };
    for (const v of UI.data.views || []) if (v.cell === cell.index) listed(v.line, v.line, () => viewHTML(cell, v.line));
    for (const e of projectEvidence(cell)) listed(e.line, e.line, () => needsProvider(e) ? null : evidenceHTML(e, staticPage));
    for (const d of UI.data.decisions || []) if (d.cell === cell.index) listed(d.line, d.end_line ?? d.line, () => needsDecider(d) ? null : decisionHTML(d, staticPage));
    const parts = [];
    let segment = [];
    const flush = () => {
      if (trimmed(segment).length) parts.push(textbookHTML(segment, scopes, true));
      segment = [];
    };
    for (let i = 0; i < entries.length; i++) {
      const directive = directives.get(entries[i][0]);
      if (!directive) { segment.push(entries[i]); continue; }
      flush();
      const {lines, card} = directive;
      const start = lines[0][0], end = lines[lines.length - 1][0];
      const html = card();
      parts.push(html ?? `<div class="cell directive-unavailable" data-document-start="${start}" data-document-end="${end}">${quietSourceHTML(lines)}${notes.get(start) || ''}</div>`);
      placed.add(start);
      i += lines.length - 1;
    }
    flush();
    return {html: parts.join(''), placed};
  }


  function kindOf(command) {
    const lines = [...command.notes, ...command.body].map(e => e[1]).filter(t => t.trim());
    if (!command.body.length) return 'context';
    const setup = lines.join('\n').replace(/\s+\([\p{L}\p{N}_'.\s]+\)$/u, '');
    if (!TRUST.test(lines.join('\n')) && /^(?:import|open(?:\s+scoped)?)(?:\s+[\p{L}_][\p{L}\p{N}_'.]*)+$/u.test(setup) && !/\bin\b/.test(lines.join('\n'))) return 'setup';
    if (lines.length === 1 && !TRUST.test(lines[0]) && !/\bin\b/.test(lines[0]) && (/^(namespace|section|end)(?:\s+(?:«[^»]+»|[\p{L}_][\p{L}\p{N}_'.]*))?\s*$/u.test(lines[0]) || /^#(?:print|check|eval)\b/.test(lines[0]))) return 'setup';
    return DECLARATION.test(command.body[0][1]) ? 'definition' : 'context';
  }

  // Namespace labels are display hints, using the same conservative command
  // boundaries as the cards. Sections affect matching `end`, not qualification.
  // If scope syntax is ambiguous, preserve it in explicit context cards.
  function namespaceScopes(cells) {
    const result = new Map();
    const stack = [];
    const ident = '(?:«[^»\\n]+»|[\\p{L}_][\\p{L}\\p{N}_\\x27]*)(?:\\.(?:«[^»\\n]+»|[\\p{L}_][\\p{L}\\p{N}_\\x27]*))*';
    const scopeHead = new RegExp(`^(namespace|section|end)(?:\\s+(${ident}))?\\s*$`, 'u');
    for (const cell of cells) {
      if (cell.kind === 'claim') {
        const namespace = stack.filter(s => s.kind === 'namespace').map(s => s.name).join('.');
        result.set(cell.index, new Map([[cell.start, {namespace}]]));
      }
      if (cell.kind !== 'lean' && cell.kind !== 'source') continue;
      const entries = cellEntries(cell);
      if (!plainComments(entries)) return new Map();
      const scopes = new Map();
      result.set(cell.index, scopes);
      for (const command of commands(entries)) {
        if (!command.body.length) continue;
        const body = leanOf(command.body).trim();
        // Multiline strings can imitate top-level commands in the line reader.
        if (command.body.some(([, line]) => (line.replace(/\\./g, '').replace(/--.*$/, '').match(/"/g) || []).length % 2)) return new Map();
        const namespace = stack.filter(s => s.kind === 'namespace').map(s => s.name).join('.');
        const info = {namespace, hide: false};
        scopes.set(command.body[0][0], info);
        if (!/^(namespace|section|end)\b/.test(body)) continue;
        if (command.notes.some(([, line]) => LEADS.test(line))) return new Map();
        const match = scopeHead.exec(body);
        if (!match) return new Map();
        const [, kind, name = ''] = match;
        if (kind === 'end') {
          const scope = stack.pop();
          if (!scope || (name && name !== scope.name)) return new Map();
          info.hide = scope.kind === 'namespace';
        } else {
          if (kind === 'namespace' && (!name || name.includes('_root_'))) return new Map();
          stack.push({kind, name});
          info.hide = kind === 'namespace';
        }
      }
    }
    return result;
  }

  const namespaceHTML = name => name ? `<p class="def-namespace">Namespace <code>${esc(name)}</code></p>` : '';

  // With `quiet`, scaffolding is shown as exact quiet lines instead of being
  // left out, and its comments as notes.
  function textbookHTML(entries, scopes = new Map(), quiet = false) {
    if (!plainComments(entries)) {
      const all = trimmed(entries);
      return all.length ? contextHTML(null, all, '') : '';
    }
    return commands(entries).map(command => {
      const list = trimmed([...command.notes, ...command.doc, ...command.body].sort((a, b) => a[0] - b[0]));
      if (!list.length) return '';
      if (quiet && command.body.length && kindOf({doc: [], notes: [], body: command.body}) === 'setup') {
        const notes = trimmed([...command.notes, ...command.doc].sort((a, b) => a[0] - b[0]));
        // A leading line such as `@[…]` belongs to the command: keep it exact.
        if (notes.some(([, line]) => LEADS.test(line))) return quietSourceHTML(list);
        return (notes.length ? contextHTML({doc: command.doc, notes: command.notes, body: []}, notes, '') : '') + quietSourceHTML(trimmed(command.body));
      }
      const kind = kindOf(command);
      const scope = scopes.get(command.body[0]?.[0]);
      // Hide only recognized scaffolding; preserve attached comments as notes.
      if (kind === 'setup' && /^(namespace|section|end)\b/.test(command.body[0][1]) && !scope) return contextHTML(command, list, '');
      if (scope?.hide || kind === 'setup') {
        const notes = trimmed([...command.notes, ...command.doc]);
        return notes.length ? contextHTML({doc: command.doc, notes: command.notes, body: []}, notes, scope?.namespace || '') : '';
      }
      if (kind === 'definition') return definitionHTML(command, list, scope?.namespace || '');
      return contextHTML(command, list, scope?.namespace || '');
    }).join('');
  }

  const sourceFoldHTML = (entries, key = 'definition', label = 'Lean source') => `<details class="def-lean" data-fold-key="${key}:${esc(leanOf(entries))}"><summary class="small muted">${esc(label)} · ${entries.length} line${entries.length === 1 ? '' : 's'}</summary><pre>${leanHTML(leanOf(entries))}</pre></details>`;

  // Translate glyphs, not meaning: identifiers, application order and grouping
  // are retained. Exact Lean stays in the source fold; no elaboration is inferred.
  function expressionHTML(text) {
    const symbols = {'∑': '\\sum', '∏': '\\prod', '*': '\\cdot', '→': '\\to', '←': '\\leftarrow', '↔': '\\leftrightarrow', '∀': '\\forall', '∃': '\\exists', '∧': '\\land', '∨': '\\lor', '¬': '\\neg', '≤': '\\le', '≥': '\\ge', '≠': '\\ne', '∈': '\\in', '∉': '\\notin', '⊆': '\\subseteq', '∪': '\\cup', '∩': '\\cap', 'ℕ': '\\mathbb{N}', 'ℤ': '\\mathbb{Z}', 'ℚ': '\\mathbb{Q}', 'ℝ': '\\mathbb{R}', '⟨': '\\langle', '⟩': '\\rangle', '×': '\\times', '∞': '\\infty', 'λ': '\\lambda', 'Σ': '\\Sigma', 'ω': '\\omega', 'ρ': '\\rho', 'ε': '\\varepsilon', 'ν': '\\nu', 'α': '\\alpha', 'β': '\\beta', 'γ': '\\gamma', 'δ': '\\delta', 'θ': '\\theta', 'π': '\\pi'};
    // Strings are typewriter text: KaTeX's text font curls a straight quote.
    const literal = (token, font = 'text') => `\\${font}{` + token.replace(/[\\{}$&#%_^~]/g, ch => ({'\\':'\\textbackslash{}','{':'\\{','}':'\\}','$':'\\$','&':'\\&','#':'\\#','%':'\\%','_':'\\_','^':'\\textasciicircum{}','~':'\\textasciitilde{}'}[ch])) + '}';
    const tokenTex = token => {
      if (Object.hasOwn(symbols, token)) return symbols[token] + ' ';
      const sub = /^(.*?)([₀-₉]+)$/.exec(token);
      if (sub) return `${tokenTex(sub[1])}_{${[...sub[2]].map(ch => '₀₁₂₃₄₅₆₇₈₉'.indexOf(ch)).join('')}}`;
      if (/^[\p{L}\p{N}_']+$/u.test(token) && /[α-ωΑ-Ω]/u.test(token)) return [...token].map(ch => Object.hasOwn(symbols, ch) ? symbols[ch] + ' ' : literal(ch)).join('');
      return /^[0-9]+$/.test(token) ? token : literal(token, token.startsWith('"') ? 'texttt' : 'text');
    };
    return text.trim().split('\n').filter(line => line.trim()).map(line => {
      const tokens = line.match(/«[^»]*»|"(?:\\.|[^"\\])*"|[\p{L}_][\p{L}\p{N}_']*|[0-9]+|\s+|./gu) || [];
      const atom = '[\\p{L}_][\\p{L}\\p{N}_\\x27.]*|[0-9]+';
      const power = new RegExp(`^(${atom})\\s*\\^\\s*(${atom})(?:\\s*/\\s*(${atom}))?$`, 'u').exec(line.trim());
      const fraction = new RegExp(`^(${atom})\\s*/\\s*(${atom})$`, 'u').exec(line.trim());
      const baseTex = tokens.map(token => /^\s+$/.test(token) ? '\\;' : /^[=+(),.:;<>!|/\-]$/.test(token) ? token : tokenTex(token)).join('');
      const tex = power ? (power[3] ? `\\frac{${tokenTex(power[1])}^{${tokenTex(power[2])}}}{${tokenTex(power[3])}}` : `${tokenTex(power[1])}^{${tokenTex(power[2])}}`) : fraction ? `\\frac{${tokenTex(fraction[1])}}{${tokenTex(fraction[2])}}` : baseTex;
      // Keep the original expression as the fallback if KaTeX is unavailable.
      return `<span class="formal-line math" data-tex="${esc(tex)}">${esc(line.trim())}</span>`;
    }).join('');
  }

  const fieldHTML = (label, value) => value ? `<div class="formal-field"><div class="field-label">${esc(label)}</div><div class="formal-expression">${expressionHTML(value)}</div></div>` : '';

  function contextHTML(command, entries, namespace) {
    const body = command ? leanOf(command.body).trim() : '';
    const context = /^(variable|universe|set_option|attribute|notation|infix[lr]?|prefix|postfix)\b\s*(.*)$/s.exec(body);
    const labels = {variable: 'Parameters', universe: 'Universes', set_option: 'Option', attribute: 'Attributes', notation: 'Notation', infix: 'Notation', infixl: 'Notation', infixr: 'Notation', prefix: 'Notation', postfix: 'Notation'};
    const note = command && !body ? leanOf([...command.notes, ...command.doc]).replace(/^\s*-- ?/gm, '').replace(/\/--?|-\//g, '').trim() : '';
    const label = context ? labels[context[1]] : note ? 'Note' : 'Additional notebook content';
    const sensitive = [...new Set(leanOf(entries).match(/\b(sorry|admit|axiom|unsafe|implemented_by|extern|native_decide)\b/g) || [])];
    return window.glitnirCards.render({
      kind: 'context', label, classes: ['context-card', ...(!context && !note ? ['unrendered'] : [])],
      locationHTML: lineLinkHTML(entries[0][0], entries[entries.length - 1][0]),
      sourceHTML: sourceFoldHTML(entries, 'context'),
      bodyHTML: `${namespaceHTML(namespace)}
      ${context ? fieldHTML('Context', context[2]) : note ? `<div class="def-body">${docHTML(note)}</div>` : '<p class="small muted">This content needs its Lean source to be read accurately; it has not been omitted.</p>'}
      ${sensitive.length ? `<p class="card-notice">Source contains ${sensitive.map(esc).join(', ')}; inspect its assumptions or unfinished construction.</p>` : ''}
      `,
    });
  }

  const charLiteralLength = (text, at) => text[at] === "'" && !/[\p{L}\p{N}_']/u.test(text[at - 1] || '') ? (/^'(?:\\[^'\n]+|[^'\\\n])'/u.exec(text.slice(at))?.[0].length || 0) : 0;

  // Remove comments only outside strings and quoted identifiers. Keeping
  // whitespace and offsets inside literals matters to the displayed meaning.
  function presentationCode(text) {
    let out = '', quoted = '', block = 0;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i], pair = text.slice(i, i + 2);
      if (quoted) {
        out += ch;
        if (quoted === '"' && ch === '\\') { out += text[++i] || ''; continue; }
        if (ch === quoted) quoted = '';
      } else if (block) {
        if (pair === '/-') { block++; i++; }
        else if (pair === '-/') { block--; i++; }
        else if (ch === '\n') out += '\n';
      } else if (charLiteralLength(text, i)) { const length = charLiteralLength(text, i); out += text.slice(i, i + length); i += length - 1; }
      else if (pair === '/-') { block++; i++; out += ' '; }
      else if (pair === '--') { while (i < text.length && text[i] !== '\n') i++; out += '\n'; }
      else { out += ch; if (ch === '"' || ch === '«') quoted = ch === '«' ? '»' : '"'; }
    }
    return out;
  }

  // Find a declaration's outer value delimiter, ignoring brackets and markers
  // inside quoted text. This is presentation only, not a Lean parser.
  function declarationParts(command) {
    const text = presentationCode(leanOf(command.body));
    let depth = 0, end = text.length, quoted = '';
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (quoted === '"' && ch === '\\') { i++; continue; }
        if (ch === quoted) quoted = '';
        continue;
      }
      if (charLiteralLength(text, i)) { i += charLiteralLength(text, i) - 1; continue; }
      if (ch === '"' || ch === '«') { quoted = ch === '«' ? '»' : '"'; continue; }
      if ('([{⟨'.includes(ch)) depth++;
      else if (')]}⟩'.includes(ch)) depth--;
      else if (depth === 0 && (text.startsWith(':=', i) || /^\n\s*\|/.test(text.slice(i, i + 40)) || (/\s/.test(text[i - 1] || ' ') && /^where(\s|$)/.test(text.slice(i, i + 6))))) { end = i; break; }
    }
    const tail = text.slice(end).trim();
    return {head: text.slice(0, end).trim(), value: tail.replace(/^(?::=|where)\s*/, ''), label: tail.startsWith('where') ? 'Fields' : tail.startsWith('|') ? 'Constructors' : 'Value'};
  }

  // A docstring is not a prose cell: paragraphs, code fences and inline code
  // only, so it adds nothing to the outline and drops no line.
  function docHTML(text) {
    const blocks = [];
    let paragraph = [];
    let fence = null;
    const inline = inlineHTML;
    const flush = () => { if (paragraph.length) blocks.push(`<p>${inline(paragraph.join(' '))}</p>`); paragraph = []; };
    for (const raw of text.split('\n')) {
      if (/^\s*```/.test(raw)) {
        if (fence) { blocks.push(`<pre>${esc(fence.join('\n'))}</pre>`); fence = null; } else { flush(); fence = []; }
        continue;
      }
      if (fence) { fence.push(raw); continue; }
      if (raw.trim()) paragraph.push(raw.trim()); else flush();
    }
    if (fence) blocks.push(`<pre>${esc(fence.join('\n'))}</pre>`);
    flush();
    return blocks.join('');
  }

  function definitionHTML(command, entries, namespace = '') {
    const match = DECLARATION.exec(command.body[0][1]);
    const [, keyword, name] = match;
    const presentation = window.glitnirCards.declarations[keyword.replace(/\s+/g, ' ')];
    const doc = leanOf(command.doc).trim().replace(/^\/--/, '').replace(/-\/$/, '').trim();
    const parts = declarationParts(command);
    const type = parts.head.slice(match[0].length).trim().replace(/^:\s*/, '');
    const text = leanOf(entries);
    const warnings = [];
    if (/\b(sorry|admit)\b/.test(text)) warnings.push('Unfinished: contains a proof placeholder.');
    if (keyword === 'axiom') warnings.push('Assumed without a proof.');
    if (/\b(unsafe|implemented_by|extern|native_decide)\b/.test(text)) warnings.push('Uses an unsafe, external or native implementation; inspect the source.');
    const proof = ['example', 'theorem', 'lemma'].includes(keyword) || /^(by\b|sorry\b|admit\b)/.test(parts.value);
    const notes = command.notes.filter(([, line]) => line.trim());
    const context = [match[0].slice(0, match[0].indexOf(keyword)).trim(), ...notes.filter(([, line]) => LEADS.test(line)).map(([, line]) => line)].filter(Boolean).join('\n');
    const comments = leanOf(notes.filter(([, line]) => !LEADS.test(line))).replace(/^\s*-- ?/gm, '').replace(/\/--?|-\//g, '').trim();
    const compact = presentation.kind === 'definition';
    const signature = parts.head.slice(match[0].length).trim();
    const headerType = compact && signature.length <= 100 && !signature.includes('\n') ? signature : '';
    const shortDoc = compact && doc.length <= 100 && !doc.includes('\n') && !doc.includes('```') ? doc : '';
    const details = compact ? `${!headerType ? fieldHTML('Type and parameters', type) : ''}
      ${doc && !shortDoc ? `<div class="def-body">${docHTML(doc)}</div>` : ''}
      ${comments ? `<div class="def-body">${docHTML(comments)}</div>` : ''}${fieldHTML('Declaration context', context)}`.trim() : '';
    const value = proof ? '<p class="small muted">Construction in Lean source.</p>'
      : parts.label === 'Value' ? `<div class="formal-expression">${expressionHTML(parts.value)}</div>` : fieldHTML(parts.label, parts.value);
    const mathematical = compact ? value
      : keyword === 'example' ? `<div class="formal-expression">${expressionHTML(type)}</div>`
      : fieldHTML(['theorem', 'lemma', 'axiom'].includes(keyword) ? 'Statement' : 'Type and parameters', type)
        + (proof ? '' : fieldHTML(/inductive/.test(keyword) ? 'Constructors' : parts.label, parts.value));
    return window.glitnirCards.render({
      kind: presentation.kind, label: presentation.label, name,
      signature: headerType, descriptionHTML: shortDoc ? inlineHTML(shortDoc) : '',
      classes: ['definition', ...(compact ? ['compact-definition'] : [])],
      locationHTML: lineLinkHTML(entries[0][0], entries[entries.length - 1][0]),
      sourceHTML: sourceFoldHTML(entries, 'definition', ['example', 'theorem', 'lemma'].includes(keyword) ? 'Lean source & proof' : 'Lean source')
        + (details ? `<details class="def-lean definition-details" data-fold-key="definition-details:${esc(leanOf(entries))}"><summary>Details</summary>${details}</details>` : ''),
      bodyHTML: `${namespaceHTML(namespace)}
      ${!compact && doc ? `<div class="def-body">${docHTML(doc)}</div>` : ''}
      ${warnings.map(warning => `<p class="card-notice">${esc(warning)}</p>`).join('')}
      ${mathematical}
      ${!compact && comments ? `<div class="def-body">${docHTML(comments)}</div>` : ''}${!compact ? fieldHTML('Declaration context', context) : ''}`,
    });
  }

  function noteFor(c) {
    const record = c.record;
    switch (c.status) {
      case 'checked': return ['green', 'check', `Checked · context ${c.context.slice(0, 8)} is current.`];
      case 'refuted': return ['red', 'x', `Refuted · Lean checked a proof of its negation in context ${c.context.slice(0, 8)}; the claim is false as stated.`];
      case 'conditional': return c.reason.startsWith('refuted assuming ')
        ? ['amber', 'branch', `Its negation checks, but only assuming ${esc(c.reason.slice('refuted assuming '.length))}.`]
        : ['amber', 'branch', `The proof checks, but it ${esc(c.reason)}.`];
      case 'stale': return ['amber', 'branch', record ? `Out of date: ${esc(c.reason)}.` : 'Not checked yet.'];
      case 'failed': return ['red', 'x', esc(c.reason)];
      case 'unknown': return ['amber', 'x', `Lean could not decide: ${esc(c.reason)}.`];
      default: return null;
    }
  }

  function claimHTML(cell, c, staticPage = false, namespace = '') {
    const st = statusOf(c, staticPage);
    const failure = UI.data.check_errors && UI.data.check_errors[c.name];
    const note = !staticPage && checking(c.name) ? null : failure ? ['red', 'x', `The last check could not run: ${esc(failure)}`] : noteFor(c);
    const uses = c.uses.length ? `<div class="uses">uses ${c.uses.map(useHTML).join('')}</div>` : '';
    return `<section class="cell" id="cell-${esc(c.name)}" data-claim="${esc(c.name)}">
      <article class="claim card-proposition" data-tone="${st.tone}" data-selected="${UI.selected === c.name}" aria-label="${esc(c.keyword)} ${esc(c.name)}">
        ${window.glitnirCards.header({label: window.glitnirCards.declarations[c.keyword]?.label || c.keyword, name: c.name, classes: ['claim-head'], locationHTML: lineLinkHTML(cell.start, cell.end), badgesHTML: `${!staticPage && checking(c.name) ? '<span class="checking-badge">Checking in Lean</span>' : ''}<span class="pill" data-tone="${st.tone}">${st.label}</span>`})}
        ${namespaceHTML(namespace)}
        ${c.doc ? `<div class="doc-line">${inlineHTML(c.doc)}</div>` : ''}
        <div class="claim-statement">${fieldHTML('Statement', c.statement.replace(/^:\s*/, ''))}</div>
        ${uses}
        ${/\b(sorry|admit)\b/.test(presentationCode(c.proof)) ? '<p class="card-notice claim-notice">Unfinished: the statement’s proof contains a placeholder.</p>' : ''}
        <div class="card-source">${sourceFoldHTML(cell.text.split('\n').map((text, i) => [cell.start + i, text]), `claim:${c.name}`, 'Lean source & proof')}${c.refutation ? `<details class="def-lean" data-fold-key="refutation:${esc(c.name)}:${esc(c.refutation.script.join('\n'))}"><summary>Recorded refutation</summary>${refutationHTML(c)}</details>` : ''}</div>
        ${note ? `<div class="note-line" data-tone="${note[0]}">${icon(note[1])}<span>${note[2]}</span></div>` : ''}

        <div class="claim-actions"><span class="small muted">${checking(c.name) ? 'Fresh sandboxed check with the independent axiom check' : c.proof.trim() === 'sorry' ? 'No proof yet' : ''}</span></div>
      </article></section>`;
  }

  // Provider views listed for this project source cell, in line order.
  function projectViewsHTML(cell, placed = new Set()) {
    return (UI.data.views || []).filter(v => v.cell === cell.index && !placed.has(v.line)).sort((a, b) => a.line - b.line).map(v => viewHTML(cell, v.line)).join('');
  }

  function cellView(cell, staticPage = false, scopes = new Map()) {
    if (cell.kind === 'prose') {
      const html = markdown(cell.markdown, `notebook-heading-${cell.index}`);
      return html ? `<section class="cell prose">${html}</section>` : '<section class="cell prose"><p class="muted">(empty prose)</p></section>';
    }
    // Blank lines between cells stay in the data for exact line ranges but are not shown.
    if (cell.kind === 'source' && !cell.text.trim()) return '';
    if (cell.kind === 'source' && cell.claim) {
      const saved = (UI.data.project_claims || []).find(c => c.name === cell.claim);
      if (saved) return projectClaimHTML(cell, saved, scopes.get(cell.index), staticPage) + sourceDirectivesHTML(cell) + projectViewsHTML(cell) + projectCardsHTML(cell, staticPage);
    }
    if (cell.kind === 'source') {
      const {html, placed} = projectSourceHTML(cell, scopes.get(cell.index), staticPage);
      const notes = nonClaimsHTML(cell) + sourceDirectivesHTML(cell, placed);
      return html + (notes ? `<div class="cell project-notes">${notes}</div>` : '') + projectViewsHTML(cell, placed) + projectCardsHTML(cell, staticPage, placed);
    }
    if (cell.kind === 'lean') return leanCellHTML(cell, scopes.get(cell.index), staticPage);
    const c = claim(cell.id);
    return c ? claimHTML(cell, c, staticPage, scopes.get(cell.index)?.get(cell.start)?.namespace) : '';
  }

  // Section structure belongs to the reading view, never to Lean checking.
  // Split prose at headings so multiple sections in one comment keep their
  // source-cell identity while their following claim/definition cells can nest.
  function proofSections(root, staticPage = false) {
    if (!root.querySelector('[data-section-fold]')) return;
    const parts = [];
    for (const node of [...root.children]) {
      const prose = node.matches('.prose') ? node : node.querySelector(':scope > .prose');
      if (!prose?.querySelector('[data-heading]')) { parts.push(node); continue; }
      let fragment, body;
      for (const child of [...prose.children]) {
        if (!body || child.hasAttribute('data-heading')) {
          body = prose.cloneNode(false);
          fragment = node === prose ? body : node.cloneNode(false);
          if (node !== prose) {
            const tools = node.querySelector(':scope > .cell-tools');
            if (tools) fragment.append(tools.cloneNode(true));
            fragment.append(body);
          }
          parts.push(fragment);
        }
        body.append(child);
      }
    }
    root.replaceChildren();
    const stack = [];
    for (const part of parts) {
      const heading = part.querySelector('[data-heading]');
      if (heading) {
        const level = Number(heading.tagName.slice(1));
        while (stack.length && stack.at(-1).level >= level) stack.pop();
        if (heading.dataset.sectionFold) {
          const section = document.createElement('details');
          section.className = 'proof-section';
          section.dataset.foldKey = 'section:' + JSON.stringify([...stack.map(s => s.title), heading.dataset.heading]);
          section.open = staticPage;
          const summary = document.createElement('summary');
          // The heading still navigates to its original source comment.
          if (part.dataset.index !== undefined) { summary.className = 'cell cellwrap'; summary.dataset.index = part.dataset.index; }
          const first = heading.nextElementSibling;
          heading.classList.add('section-title');
          summary.append(heading);
          if (first?.tagName === 'P') {
            const abstract = document.createElement('span');
            abstract.className = 'section-summary';
            abstract.append(...first.childNodes);
            summary.append(abstract);
            first.remove();
          }
          const body = document.createElement('div');
          body.className = 'proof-section-body';
          const tools = part.querySelector(':scope > .cell-tools');
          if (tools) { tools.className = 'section-tools'; summary.append(tools); }
          section.append(summary, body);
          (stack.at(-1)?.body || root).append(section);
          stack.push({level, title: heading.dataset.heading, body});
        }
      }
      const prose = part.matches('.prose') ? part : part.querySelector(':scope > .prose');
      if (!prose || prose.children.length) (stack.at(-1)?.body || root).append(part);
    }
    const fragments = new Map();
    for (const wrap of root.querySelectorAll('.cellwrap')) {
      const index = wrap.dataset.index;
      const fragment = fragments.get(index) || 0;
      wrap.dataset.fragment = fragment;
      fragments.set(index, fragment + 1);
    }
    for (const section of root.querySelectorAll('details.proof-section')) {
      const counts = new Map();
      for (const card of section.querySelectorAll('[data-claim]')) {
        const c = claim(card.dataset.claim);
        if (!c) continue;
        for (const status of summariesOf(c, staticPage)) {
          const count = counts.get(status.key);
          counts.set(status.key, {...status, count: (count?.count || 0) + 1});
        }
      }
      const badges = document.createElement('span');
      badges.className = 'section-statuses';
      badges.setAttribute('aria-label', 'Claim observations in this section, including subsections');
      badges.innerHTML = counts.size ? [...counts.values()].map(s => `<span class="pill" data-tone="${esc(s.tone)}">${s.count} ${esc(s.label)}</span>`).join(' ') : '<span class="muted">No claim observations</span>';
      section.querySelector(':scope > summary').append(badges);
    }
  }

  function revealSection(target) {
    for (let node = target; node; node = node.parentElement) {
      if (node.matches('details.proof-section')) node.open = true;
    }
  }

  function openSource(index) {
    const cell = UI.data && UI.data.cells.find(c => c.index === index);
    if (cell && HOST) HOST.openSource({index: cell.index, sha256: UI.data.sha256});
  }
  if (HOST) {
    HOST.openSelectedSource = () => {
      if (linkedPath()) { UI.notice = `${linkedPath()} is read-only here. Open it in its own preview to edit or check it.`; render(); return; }
      const cells = UI.data ? UI.data.cells : [];
      const cell = cells.find(c => UI.sourceCell && UI.sourceCell.sha256 === UI.data.sha256 && c.index === UI.sourceCell.index)
        || cells.find(c => c.id === UI.selected || (c.claim && c.claim === UI.selected)) || cells[0];
      if (cell) openSource(cell.index); else HOST.openSource({});
    };
    document.addEventListener('click', event => {
      const wrap = event.target.closest('.cellwrap');
      if (wrap) UI.sourceCell = {index: Number(wrap.dataset.index), sha256: UI.data.sha256};
    }, true);
    document.addEventListener('glitnir-reveal-cell', async event => {
      HOST.pendingReveal = null;
      // A reveal names a place in the previewed notebook.
      if (linkedPath()) showNotebookAt(0);
      if (!UI.data || UI.data.sha256 !== event.detail.sha256) await refresh();
      if (!UI.data || UI.data.sha256 !== event.detail.sha256) return;
      UI.mode = 'doc';
      UI.sourceCell = {index: event.detail.index, sha256: event.detail.sha256};
      const cell = UI.data.cells.find(c => c.index === UI.sourceCell.index);
      if (!cell) return;
      if (cell.kind === 'claim') UI.selected = cell.id;
      else if (cell.claim && claim(cell.claim)) UI.selected = cell.claim;
      render();
      const target = document.querySelector(`.cellwrap[data-index="${cell.index}"]`);
      if (target) { revealSection(target); target.classList.add('source-selected'); target.scrollIntoView({block: 'center'}); }
      else showSourceLines(cell.start, cell.end);
    });
    if (HOST.pendingReveal) document.dispatchEvent(new CustomEvent('glitnir-reveal-cell', {detail: HOST.pendingReveal}));
  }

  function toolsHTML(cell) {
    return `<div class="cell-tools">${lineLinkHTML(cell.start, cell.end)}</div>`;
  }

  function renderDoc() {
    const scopes = namespaceScopes(UI.data.cells);
    $('doc').innerHTML = withheldBannerHTML(UI.data) + UI.data.cells.map(cell => {
      const body = cellView(cell, false, scopes);
      return body ? `<div class="cellwrap" data-index="${cell.index}">${toolsHTML(cell)}${body}</div>` : '';
    }).join('');
    proofSections($('doc'));
    if (HOST) for (const wrap of $('doc').querySelectorAll('.cellwrap')) {
      if (wrap.querySelector('.cell .source-controls')) wrap.querySelector(':scope > .cell-tools')?.remove();
    }
  }

  function renderSource() {
    if (UI.sourceRange?.sha !== UI.data.sha256) UI.sourceRange = null;
    const range = UI.sourceRange;
    const rows = [];
    UI.data.cells.forEach(cell => {
      const c = cell.kind === 'claim' ? claim(cell.id) : cell.claim ? claim(cell.claim) : null;
      cell.text.split('\n').slice(0, cell.end - cell.start + 1).forEach((line, i) => rows.push({n: cell.start + i, line, c}));
    });
    const back = `<button type="button" class="secondary" data-act="document-return" data-sha="${esc(UI.data.sha256)}">← Back to Document</button>`;
    $('source').innerHTML = `${range ? '' : `<p>${back}</p>`}<p class="source-note">${esc(UI.data.file)} as it is on disk ${UI.data.preview?.mode === 'project' ? '· source preview; the gutter marks saved results · use Check saved file to check it' : '· the gutter shows each claim\'s status'} · click a line number to show its place in Document.</p>
      <div class="source"><table>${rows.map(r => `${range && r.n === range.start ? `<tr class="source-return"><td colspan="3">${back}</td></tr>` : ''}<tr id="source-L${r.n}" tabindex="-1" data-source-line="${r.n}" data-line-target="${Boolean(range && r.n >= range.start && r.n <= range.end)}" ${r.c ? `data-claim="${esc(r.c.name)}" data-tone="${toneOf(r.c)}" data-selected="${UI.selected === r.c.name}"` : ''}><td class="gut"></td><td class="ln"><a href="#doc" data-act="document-line" data-line="${r.n}" data-sha="${esc(UI.data.sha256)}" aria-label="Show line ${r.n} in Document">${r.n}</a></td><td>${leanHTML(r.line) || ' '}</td></tr>`).join('')}</table></div>`;
  }

  Object.assign(workbench, {cellEntries, cellView, commands, declarationParts, docHTML, fieldHTML, leanOf,
    lineLinkHTML, namespaceHTML, namespaceScopes, openSource, plainComments, proofSections, renderDoc,
    renderSource, revealSection, sourceFoldHTML, textbookHTML, trimmed, unevenQuotes});
})();
