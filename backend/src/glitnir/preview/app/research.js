// Bounded, observational Taskvalet history beside a project claim.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, API, UI, claim, esc, fetch, icon} = workbench;
  const render = (...a) => workbench.render(...a);
  const CATEGORY = {
    checked_target: ['Checked target', 'green'], checked_helper: ['Checked helper', 'green'],
    reported_target_check: ['Reported target check', 'blue'], reported_helper_check: ['Reported helper check', 'blue'],
    unchecked_candidate: ['Unchecked candidate', 'blue'], numerical_evidence: ['Numerical evidence', 'amber'],
    refutation: ['Refutation', 'red'], failed_approach: ['Failed approach', 'red'],
    inconclusive: ['Inconclusive', 'amber'], stale_or_unavailable: ['Unavailable or stale', 'amber'],
  };
  const linked = name => UI.data?.research?.[name];
  const bindingKey = name => JSON.stringify([UI.data?.file || '', UI.data?.sha256 || '', linked(name) || null]);

  async function loadResearch(name, force = false) {
    const binding = linked(name);
    const key = bindingKey(name), cached = UI.research[name];
    if (!claim(name)?.project || !binding || UI.researchLoading[name] || (!force && cached?.key === key)) return;
    const source = UI.data.sha256;
    UI.researchLoading[name] = key;
    render();
    try {
      const response = await fetch(`/api/research?claim=${encodeURIComponent(name)}`, {cache: 'no-store', headers: API});
      const answer = await response.json().catch(() => ({}));
      if (UI.data?.sha256 !== source || bindingKey(name) !== key) return;
      UI.research[name] = {key, history: response.ok ? answer : {state: 'unavailable', reason: answer.error || `The server answered ${response.status}.`, rounds: [], partial: [], binding}};
    } catch (_) {
      if (UI.data?.sha256 === source && bindingKey(name) === key) UI.research[name] = {key, history: {state: 'disconnected', reason: 'Taskvalet did not answer. Refresh once it is available.', rounds: [], partial: [], binding}};
    } finally {
      const changed = bindingKey(name) !== key;
      if (UI.researchLoading[name] === key) UI.researchLoading[name] = false;
      if (UI.selected === name) render();
      if (changed) void loadResearch(name);
    }
  }

  function evidenceSheet(name, history) {
    const selected = UI.researchEvidence[name];
    let evidence;
    for (const round of history.rounds || []) {
      evidence = (round.evidence || []).find(item => `${round.id}:${item.id}` === selected);
      if (evidence) break;
    }
    if (!evidence) return '';
    const category = CATEGORY[evidence.category] || [evidence.category || 'Observation', 'muted'];
    const source = evidence.receipt?.job?.request?.source;
    const record = evidence.record || {}, finding = evidence.finding || {}, receipt = evidence.receipt;
    const assignment = evidence.assignment || {}, origin = evidence.originating_finding || {};
    return `<section class="research-evidence-sheet" aria-label="Selected research evidence"><header><span class="pill" data-tone="${category[1]}">${esc(category[0])}</span><button type="button" class="ghost" data-act="research-evidence-close" aria-label="Close evidence details">${icon('x')}</button></header>
      <p>${esc(evidence.reason || '')}</p><dl><dt>record</dt><dd>${esc(evidence.id || 'unavailable')} · v${esc(evidence.version || '?')}</dd><dt>producer</dt><dd>${esc(evidence.author || 'unknown')}</dd><dt>assignment</dt><dd>${esc(assignment.id || 'unavailable')}</dd><dt>task</dt><dd>${esc(evidence.task || assignment.data?.task || 'unavailable')}</dd></dl>
      ${origin.id && origin.id !== evidence.id ? `<p><b>Originating finding</b></p><dl><dt>record</dt><dd>${esc(origin.id)} · v${esc(origin.version || '?')}</dd><dt>producer</dt><dd>${esc(origin.author || 'unknown')}</dd><dt>task</dt><dd>${esc(origin.task || 'unavailable')}</dd></dl>` : ''}
      ${evidence.selected_reason ? `<p class="research-selection"><b>Why it moved forward</b><br>${esc(evidence.selected_reason)}</p>` : ''}
      ${source ? `<details><summary>Exact submitted source</summary><pre>${esc(source)}</pre></details>` : ''}<details><summary>Exact Taskvalet selected record</summary><pre>${esc(JSON.stringify(record, null, 2))}</pre></details>${finding.id && finding.id !== record.id ? `<details><summary>Exact originating finding</summary><pre>${esc(JSON.stringify(finding, null, 2))}</pre></details>` : ''}${receipt ? `<details><summary>Exact check receipt</summary><pre>${esc(JSON.stringify(receipt, null, 2))}</pre></details>` : ''}
      ${(evidence.consumer_checks || []).length ? `<details><summary>Consumer checks (${evidence.consumer_checks.length})</summary>${evidence.consumer_checks.map(check => `<div class="research-consumer"><b>${esc(check.method || 'review')} · ${esc(check.verdict || 'unknown')}</b><p>${esc(check.text || '')}</p><code>${esc(check.id || '')}</code></div>`).join('')}</details>` : ''}
      ${(evidence.artifacts || []).length ? `<details><summary>Pinned artifacts (${evidence.artifacts.length})</summary><ul>${evidence.artifacts.map(a => `<li><span>${esc(a.uri || '')}</span><code>${esc(a.revision || '')}</code></li>`).join('')}</ul></details>` : ''}</section>`;
  }

  function researchHTML(c) {
    const binding = linked(c.name);
    if (!binding) return UI.data?.research_error ? `<div class="insp-section research-state" data-state="unavailable"><h5>Research history unavailable</h5><p>${esc(UI.data.research_error)}</p></div>` : '';
    const loading = UI.researchLoading[c.name], cached = UI.research[c.name];
    const history = cached?.key === bindingKey(c.name) ? cached.history : null;
    if (loading && !history) return '<div class="insp-section research-state" role="status"><h5>Investigation</h5><p>Loading Taskvalet history…</p></div>';
    if (!history) return `<div class="insp-section research-state"><h5>Investigation</h5><button type="button" class="secondary" data-act="research-refresh" data-id="${esc(c.name)}">Load history</button></div>`;
    const b = history.binding || binding, problem = history.problem || {}, historyRounds = Array.isArray(history.rounds) ? history.rounds : [];
    const stateText = {ready: 'Current', stale: 'Source binding changed', partial: 'Partial history', unavailable: 'History unavailable', disconnected: 'Service disconnected'}[history.state] || history.state;
    const rounds = historyRounds.map((round, index) => {
      const decision = round.decision || {};
      const evidence = (round.evidence || []).map(item => { const category = CATEGORY[item.category] || [item.category || 'Observation', 'muted']; const key = `${round.id}:${item.id}`; return `<button type="button" class="research-evidence-row" data-act="research-evidence" data-id="${esc(c.name)}" data-key="${esc(key)}" aria-pressed="${UI.researchEvidence[c.name] === key}"><span class="research-evidence-mark" data-tone="${category[1]}"></span><span><b>${esc(category[0])}</b><small>${esc((item.summary || '').slice(0, 180) || item.reason || 'No summary')}</small></span></button>`; }).join('');
      const participants = (round.participants || []).map(p => `<li><b>${esc(p.role || 'participant')}</b><span>${esc(p.task || 'unavailable')} · ${esc(p.status || 'unknown')}</span>${p.assignment_title ? `<small>${esc(p.assignment_title)}</small>` : ''}</li>`).join('');
      const obstacles = (decision.obstacles || []).map(item => `<li>${esc(item)}</li>`).join(''), allocations = (decision.allocations || []).map(item => `<li><b>${esc(item.mode || 'allocated')}</b> ${esc(item.task || '')}${item.approach ? ` · ${esc(item.approach)}` : ''}</li>`).join('');
      const exploration = Number.isFinite(decision.exploration_percent) ? `<p class="muted">Exploration allocation: ${esc(decision.exploration_percent)}%</p>` : '';
      return `<li class="research-round" data-state="${esc(round.status || 'unknown')}"><span class="research-spine" aria-hidden="true"></span><details ${index === historyRounds.length - 1 ? 'open' : ''}><summary><span><b>Round ${Number(history.truncated_rounds || 0) + index + 1}</b><small>${esc(round.approach || 'Approach unavailable')}</small></span><span class="pill" data-tone="${round.status === 'review' ? 'amber' : round.status === 'finished' ? 'green' : 'muted'}">${esc(round.status || 'unknown')}</span></summary><div class="research-round-body">${round.instructions ? `<p>${esc(round.instructions)}</p>` : ''}${participants ? `<h6>Team and assignments</h6><ul class="research-participants">${participants}</ul>` : ''}${decision.action ? `<div class="research-decision"><b>${esc(decision.action.replaceAll('_', ' '))}</b>${decision.assessment ? ` · ${esc(decision.assessment.replaceAll('_', ' '))}` : ''}${decision.rationale ? `<p>${esc(decision.rationale)}</p>` : ''}${obstacles ? `<h6>Remaining obstacles</h6><ul>${obstacles}</ul>` : ''}${decision.uncertainty ? `<p class="muted">${esc(decision.uncertainty)}</p>` : ''}${exploration}${allocations ? `<h6>Allocation</h6><ul>${allocations}</ul>` : ''}</div>` : ''}${evidence ? `<h6>Evidence handed forward</h6><div class="research-evidence-list">${evidence}</div>` : '<p class="small muted">No evidence handoff saved for this round.</p>'}</div></details></li>`;
    }).join('');
    const unavailable = ['unavailable', 'disconnected'].includes(history.state);
    return `<div class="insp-section research-history" aria-label="Taskvalet investigation history"><div class="research-history-head"><h5>Investigation</h5><button type="button" class="secondary" data-act="research-refresh" data-id="${esc(c.name)}" ${loading ? 'disabled' : ''}>${loading ? 'Refreshing…' : 'Refresh'}</button></div><div class="research-state" data-state="${esc(history.state)}" role="status"><b>${esc(stateText)}</b>${history.reason ? `<span>${esc(history.reason)}</span>` : ''}</div>${unavailable ? `<p>${esc(history.reason || 'History is unavailable.')}</p>` : `<details class="research-binding"><summary>Claim and environment binding</summary><dl><dt>declaration</dt><dd>${esc(b.declaration)}</dd><dt>type</dt><dd>${esc(b.type)}</dd><dt>context</dt><dd>${esc(b.context_sha256)}</dd><dt>source</dt><dd>${esc(b.source_sha256)}</dd><dt>environment</dt><dd>${esc(Object.entries(b.environment || {}).map(([k, v]) => `${k}: ${v}`).join(', '))}</dd><dt>axiom policy</dt><dd>${esc((b.axiom_policy || []).join(', ') || 'no axioms')}</dd></dl></details><div class="research-problem"><h6>${esc(problem.title || 'Original research problem')}</h6><p>${esc(problem.statement || 'Statement unavailable')}</p><b>Assumptions</b><p>${esc(problem.assumptions || 'Assumptions unavailable')}</p></div>${(history.partial || []).map(reason => `<p class="research-partial">${esc(reason)}</p>`).join('')}${history.truncated_rounds ? `<p class="research-partial">${esc(history.truncated_rounds)} older rounds are outside this bounded view.</p>` : ''}<ol class="research-rounds">${rounds || '<li class="research-empty">No admitted rounds are saved for this claim.</li>'}</ol>${evidenceSheet(c.name, history)}<p class="small muted research-authority">Campaign completion and allocation advice never change this claim’s proof status.</p>`}</div>`;
  }

  function researchAction(button) {
    const id = button.dataset.id;
    if (button.dataset.act === 'research-refresh') { void loadResearch(id, true); return true; }
    if (button.dataset.act === 'research-evidence') { UI.researchEvidence[id] = button.dataset.key; render(); const close = $('insp').querySelector('[data-act=research-evidence-close]'); close?.focus({preventScroll: true}); close?.closest('.research-evidence-sheet')?.scrollIntoView({block: 'nearest'}); return true; }
    if (button.dataset.act === 'research-evidence-close') { const key = UI.researchEvidence[UI.selected]; UI.researchEvidence[UI.selected] = null; render(); const row = $('insp').querySelector(`[data-act=research-evidence][data-key="${CSS.escape(key || '')}"]`); row?.focus({preventScroll: true}); row?.scrollIntoView({block: 'nearest'}); return true; }
    return false;
  }

  Object.assign(workbench, {loadResearch, researchAction, researchHTML});
})();
