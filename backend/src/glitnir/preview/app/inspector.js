// The workbench's claim inspector and proof goals.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, API, UI, cellEntries, checkThroughHTML, checkedStatementHTML, claim, claimReading, claims,
    currencyNoted, currencyPillHTML, currencyText, docHTML, environmentEpoch, esc, fetch, icon, inlineHTML,
    leanOff, leanVersion, lineLinkHTML, linkedPath, projectMode, recorded, refresh, statusOf, title, trimmed,
    useChipsHTML} = workbench;
  // Defined by a later file and bound when it loads: nothing may call one while the page is still loading.
  const render = (...a) => workbench.render(...a);
  // ----- inspector ------------------------------------------------------------------
  function refutationHTML(c) {
    return c.refutation ? `<div class="insp-section"><h5>Refutation${c.refutation.context === c.context ? '' : ' · old context'}</h5><p class="small">A proof of <code>¬ ${esc(c.name)}</code>, recorded beside the notebook; last check ${esc(c.refutation.verdict)}.</p><pre>${esc(':= by\n  ' + c.refutation.script.join('\n  '))}</pre></div>` : '';
  }

  // A saved project claim: its record's status, currency and assumptions by
  // module, and the docstring of its exact source.
  function projectInspectorHTML(c) {
    const saved = c.project;
    const st = statusOf(c);
    const cell = UI.data.cells.find(cell => cell.claim === c.name);
    const reading = cell ? claimReading(trimmed(cellEntries(cell))) : null;
    const modules = new Map();
    for (const axiom of saved.axioms) modules.set(axiom.module, [...(modules.get(axiom.module) || []), axiom.name]);
    const statement = saved.example === true ? checkedStatementHTML(saved, true) : '';
    const checked = statement ? `<div class="insp-section">${statement}</div>` : '';
    return `<div class="focus"><div class="row"><span class="pill" data-tone="${st.tone}">${esc(st.label)}</span>${currencyPillHTML(saved)}</div>${reading?.doc ? `<div class="eq">${docHTML(reading.doc)}</div>` : ''}
        <dl><dt>name</dt><dd>${esc(saved.example === true ? c.label : c.name)}</dd><dt>lines</dt><dd>${cell ? lineLinkHTML(saved.start_line, saved.end_line) : '—'}</dd><dt>result</dt><dd>${currencyText(saved)}</dd><dt>uses</dt><dd class="insp-uses">${!Array.isArray(saved.uses) ? '<span class="muted">not recorded by this check</span>' : saved.uses.length ? `<span class="uses">${useChipsHTML(saved)}</span>` : '—'}</dd></dl></div>
      ${checked}
      ${linkedPath() ? '' : `<div class="insp-section insp-check">${checkThroughHTML(saved, 'Check through this claim', `<kbd>r</kbd> · compiles and kernel-replays lines 1–${esc(saved.end_line)} of the saved file; later claims are not rechecked. <b>Check saved file</b> checks all of it.`)}</div>`}
      <div class="insp-section"><h5>Assumptions from the last check</h5>${modules.size ? [...modules].map(([module, names]) => `<p class="small muted axiom-module">${esc(module)}</p><div class="axioms">${names.map(name => `<code>${esc(name)}</code>`).join('')}</div>`).join('') : '<p class="small muted">None.</p>'}</div>
      ${goalSectionHTML(c)}`;
  }

  // ----- proof goals ------------------------------------------------------------------
  // The goals Lean shows at each tactic step of a claim's saved proof, from the
  // same observation as Proof steps: one runs at a time and records the whole
  // saved file, so one observation serves every claim. It is historical: it
  // never changes a status, and a source or dependency change makes it stale.
  let goalTimer, goalEpoch = 0;
  const MAX_GOAL_STEPS = 200;
  const traceRunning = trace => trace?.status === 'running';
  const traceFinished = trace => ['ready', 'partial'].includes(trace?.status);
  // The dependency epoch in which this page first saw each observation. An
  // observation seen only before a dependency change describes the old state.
  const traceSeen = new Map();
  async function readTrace() {
    const response = await fetch('/api/proof/trace', {cache: 'no-store', headers: API});
    const answer = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(answer.error || `the server answered ${response.status}`);
    // First seen after a dependency change: when it was recorded is unknown.
    if (answer.id && !traceSeen.has(answer.id)) traceSeen.set(answer.id, environmentEpoch() === 0 ? 0 : -1);
    return answer;
  }
  // A finished observation of this saved text is reused only while no
  // dependency changed since this page first saw it, or since it started it.
  // The server and this page start together, so one first seen before any
  // change was recorded under the current dependencies.
  const reusable = (trace, sha256) => traceFinished(trace) && trace.source_sha256 === sha256 && trace.id && traceSeen.get(trace.id) === environmentEpoch();
  async function inspectGoals(refresh = false) {
    if (!UI.data || linkedPath() || !projectMode() || leanOff() || UI.goals?.starting || traceRunning(UI.goals?.trace)) return;
    const sha256 = UI.data.sha256, epoch = ++goalEpoch, environment = environmentEpoch();
    clearTimeout(goalTimer);
    UI.goals = {starting: true, environment, trace: {status: 'running', source_sha256: sha256, message: 'Preparing proof goals', nodes: []}};
    render();
    try {
      // Reuse a finished observation of this saved text, or join a running
      // one (for example one Proof steps started); otherwise start one. Only
      // an observation started here is cancelled from here.
      let trace = await readTrace();
      let owned = false;
      const joined = traceRunning(trace) && trace.source_sha256 === sha256 && trace.id;
      if (!joined && (refresh || !reusable(trace, sha256))) {
        const response = await fetch('/api/proof/start', {method: 'POST', headers: {...API, 'Content-Type': 'application/json'}, body: JSON.stringify({sha256})});
        trace = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(trace.error || `the server answered ${response.status}`);
        if (trace.id) traceSeen.set(trace.id, environmentEpoch());
        owned = true;
      }
      if (epoch !== goalEpoch) return;
      const cancel = UI.goals.cancel;
      UI.goals = {environment, trace, owned};
      render();
      if (cancel && traceRunning(trace)) { cancelGoals(); if (!owned) return; }
      if (traceRunning(trace)) goalTimer = setTimeout(() => pollGoals(trace.id, epoch), 750);
    } catch (error) {
      if (epoch !== goalEpoch) return;
      UI.goals = {environment, trace: {status: 'unavailable', source_sha256: sha256, message: error.message || String(error), nodes: []}};
      render();
    }
  }
  async function pollGoals(id, epoch) {
    try {
      const trace = await readTrace();
      if (epoch !== goalEpoch) return;
      if (trace.id !== id) throw new Error('Another proof observation replaced this one. Inspect the goals again.');
      UI.goals = {...UI.goals, trace};
      render();
      if (traceRunning(trace)) goalTimer = setTimeout(() => pollGoals(id, epoch), 750);
    } catch (error) {
      if (epoch !== goalEpoch) return;
      UI.goals = {...UI.goals, trace: {status: 'unavailable', source_sha256: UI.goals.trace.source_sha256, message: error.message || String(error), nodes: []}};
      render();
    }
  }
  async function cancelGoals() {
    if (!traceRunning(UI.goals?.trace)) return;
    // Asked before the observation started: cancel it once it has.
    if (UI.goals.starting) { UI.goals = {...UI.goals, cancel: true, trace: {...UI.goals.trace, message: 'Cancelling'}}; render(); return; }
    if (!UI.goals.owned) {
      // Joined, not started here: stop following it and leave it running.
      goalEpoch++;
      clearTimeout(goalTimer);
      UI.goals = {...UI.goals, trace: {status: 'detached', source_sha256: UI.goals.trace.source_sha256, nodes: []}};
      render();
      return;
    }
    UI.goals = {...UI.goals, trace: {...UI.goals.trace, message: 'Cancelling'}};
    render();
    // Cancelling only stops work, so unsaved edits never hold it back.
    await fetch('/api/proof/cancel', {method: 'POST', headers: {...API, 'Content-Type': 'application/json'}, body: '{}'}).catch(() => null);
  }
  const goalBlocksHTML = goals => goals.length
    ? goals.map(goal => `<pre class="goal goal-block">${esc(goal)}</pre>`).join('')
    : '<pre class="goal" data-tone="green">No goals remain in this tactic’s scope.</pre>';
  function goalSectionHTML(c) {
    const head = '<h5>Proof goals</h5>';
    if (linkedPath()) return `<div class="insp-section goal-section">${head}<p class="small muted">Open this notebook in its own preview to inspect its proof goals.</p></div>`;
    if (leanOff()) return `<div class="insp-section goal-section">${head}<p class="small muted">Proof steps need Lean ${esc(leanVersion(UI.data.preview.lean.toolchain))}. The saved proof stays readable.</p></div>`;
    const goals = UI.goals, trace = goals?.trace;
    const button = label => `<button type="button" class="secondary" data-act="goal" data-id="${esc(c.name)}"${label.startsWith('Refresh') ? ' data-refresh="true"' : ''}>${label}</button>`;
    const note = (tone, text) => `<div class="note-line goal-note" data-tone="${tone}">${icon(tone === 'red' ? 'x' : 'branch')}<span>${text}</span></div>`;
    const historical = '<p class="small muted goal-caption">A historical observation of the saved proof: it never changes a status.</p>';
    let body;
    if (!trace) body = `${button('Inspect proof goals')}<p class="small muted goal-caption">Shows the goals Lean reports at each tactic step of this claim’s saved proof. A historical observation: it never changes a status.</p>`;
    else if (traceRunning(trace)) body = `<p class="small muted goal-state" role="status">Asking Lean… ${esc(trace.message || '')}</p><button type="button" class="secondary" data-act="goal-cancel">Cancel</button>${historical}`;
    else if (trace.source_sha256 !== UI.data.sha256) body = note('amber', 'The saved source changed since these goals were recorded.') + button('Refresh proof goals');
    else if (goals.environment !== environmentEpoch()) body = note('amber', 'The source or its dependencies changed since these goals were recorded.') + button('Refresh proof goals');
    else if (trace.status === 'detached') body = note('muted', 'Stopped following the proof observation Proof steps started; it keeps running there.') + button('Inspect proof goals');
    else if (trace.status === 'cancelled') body = note('muted', 'The proof observation was cancelled. No goals were recorded.') + button('Inspect proof goals');
    else if (!['ready', 'partial'].includes(trace.status)) body = note('red', `No proof goals: ${esc(trace.message || trace.status)}`) + button('Inspect proof goals');
    else body = goalStepsHTML(c, trace) + button('Refresh proof goals');
    return `<div class="insp-section goal-section">${head}${body}</div>`;
  }
  function goalStepsHTML(c, trace) {
    const {start_line: start, end_line: end} = c.project;
    const byId = new Map(trace.nodes.map(n => [n.id, n]));
    const nodes = trace.nodes.filter(n => n.range.start.line >= start && n.range.start.line <= end);
    const messages = [...(trace.errors || []), ...(trace.warnings || [])];
    const messagesHTML = messages.length ? `<details class="goal-messages" data-fold-key="goal-messages"><summary class="small muted">Lean messages for the file · ${messages.length}</summary><pre>${esc(messages.join('\n\n'))}</pre></details>` : '';
    const provenance = `<p class="small muted goal-caption">Recorded from source ${esc(String(trace.source_sha256).slice(0, 8))}${trace.environment?.toolchain ? ` with ${esc(trace.environment.toolchain)}` : ''}. Dependencies were read-only live inputs. Recorded elaboration attempts, not a proof certificate: this never changes a status.</p>`;
    if (!nodes.length) {
      return `<div class="note-line goal-note" data-tone="muted">${icon('branch')}<span>No tactic steps were recorded for this claim. It may be a term proof${trace.status === 'partial' ? ', or Lean stopped before it' : ''}.</span></div>${messagesHTML}${provenance}`;
    }
    const depth = n => { let d = 0; for (let p = n.parent; p !== null && p !== undefined && d < 6; p = byId.get(p)?.parent) d++; return d; };
    const shown = nodes.slice(0, MAX_GOAL_STEPS);
    const steps = shown.map((n, i) => {
      const lines = n.text.split('\n');
      const tactic = lines[0] + (lines.length > 1 ? ' …' : '');
      return `<li class="goal-step" data-depth="${depth(n)}"><div class="goal-tactic"><span class="goal-number">${i + 1}</span><code title="${esc(n.text)}">${esc(tactic)}</code><span class="small muted">L${esc(n.range.start.line)}</span></div>${goalBlocksHTML(n.after)}</li>`;
    }).join('');
    return (trace.status === 'partial' ? `<div class="note-line goal-note" data-tone="amber">${icon('branch')}<span>Lean reported errors in the file; some steps may be missing.</span></div>` : '')
      + `<div class="goal-initial"><div class="field-label">Initial goal</div>${goalBlocksHTML(nodes[0].before)}</div>`
      + `<ol class="goal-steps" aria-label="Goals after each tactic step">${steps}</ol>`
      + (nodes.length > shown.length ? `<p class="small muted">Showing the first ${shown.length} of ${nodes.length} steps. Proof steps shows them all.</p>` : '')
      + messagesHTML + provenance;
  }

  function renderInspector() {
    const c = claim(UI.selected);
    if (!c) {
      const project = projectMode();
      $('insp-title').textContent = project && UI.data.cells.length ? 'No saved results' : 'No claims';
      $('insp-sub').textContent = '';
      $('insp').innerHTML = project
        ? `<p class="small muted">No claim of this file has a saved result.${leanOff() ? '' : ' Check the saved file to see each claim\'s status here.'}</p>`
        : '<p class="small muted">This notebook has no theorems yet.</p>';
      return;
    }
    if (c.project) {
      $('insp-title').textContent = c.label;
      $('insp-sub').textContent = `${c.keyword || 'claim'} · ${statusOf(c).label.toLowerCase()}${currencyNoted(c.project) ? ` · ${currencyText(c.project)}` : ''}`;
      $('insp').innerHTML = projectInspectorHTML(c);
      return;
    }
    const st = statusOf(c);
    const r = c.record;
    $('insp-title').textContent = c.name;
    $('insp-sub').textContent = `${c.keyword} · ${st.label.toLowerCase()}`;
    $('insp').innerHTML = `
      <div class="focus"><div class="row"><span class="pill" data-tone="${st.tone}">${st.label}</span></div>${c.doc ? `<div class="eq">${inlineHTML(c.doc)}</div>` : ''}
        <dl><dt>context</dt><dd>${c.context.slice(0, 12)}</dd><dt>last check</dt><dd>${r ? `${esc(r.verdict)}${r.context === c.context ? '' : ' · old context'}` : 'none'}</dd><dt>uses</dt><dd>${esc(c.uses.join(', ') || '—')}</dd></dl></div>
      ${c.reason ? `<div class="insp-section"><h5>Why</h5><p class="small">${esc(c.reason)}</p></div>` : ''}
      ${refutationHTML(c)}
      ${r && r.axioms.length ? `<div class="insp-section"><h5>Axioms from the last check</h5><div class="axioms">${r.axioms.map(a => `<code>${esc(a)}</code>`).join('')}</div></div>` : ''}
`;
  }

  Object.assign(workbench, {cancelGoals, inspectGoals, refutationHTML, renderInspector});
})();
