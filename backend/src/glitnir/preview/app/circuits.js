// Presentation of a provider's typed circuit graph. Never reconstruct topology from SVG or
// infer a proof from a label: contracts come from the Lean view adapter.
(() => {
    'use strict';
    const esc = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const number = n => Number.isSafeInteger(n) && n >= 0 && n <= 100000;
    const text = s => typeof s === 'string' && s.length <= 8192;
    const kinds = new Set(['identity', 'constant', 'scalar', 'add', 'multiply', 'split', 'swap', 'terminal', 'integrate']);
    const line = (x1, y1, x2, y2, cls = 'circuit-wire') => `<path class="${cls}" d="M${x1} ${y1}L${x2} ${y2}"/>`;
    const label = (x, y, s, cls = '', anchor = 'start') => `<text class="${cls}" x="${x}" y="${y}" text-anchor="${anchor}">${esc(s)}</text>`;
    const short = s => s.length > 24 ? s.slice(0, 21) + '…' : s;
    function validGraph(g) {
        if (!g || !Array.isArray(g.nodes) || g.nodes.length > 2048 || !Array.isArray(g.edges) || g.edges.length > 8192)
            return false;
        if (!g.nodes.every(n => n && kinds.has(n.kind) && text(n.label) && number(n.inputs) && n.inputs <= 64 && number(n.outputs) && n.outputs <= 64 && number(n.x) && number(n.y)))
            return false;
        const endpoint = (e, out) => Array.isArray(e) && e.length === 2 && number(e[0]) && number(e[1]) && e[0] < g.nodes.length && e[1] < g.nodes[e[0]][out ? 'outputs' : 'inputs'];
        return Array.isArray(g.inputs) && g.inputs.length <= 2048 && g.inputs.every(e => endpoint(e, false)) && Array.isArray(g.outputs) && g.outputs.length <= 2048 && g.outputs.every(e => endpoint(e, true)) && g.edges.every(e => e && endpoint(e.source, true) && endpoint(e.target, false) && typeof e.feedback === 'boolean' && number(e.lane));
    }
    function contractsOf(data) {
        if (!Array.isArray(data.contracts) || data.contracts.length > 2048 || !validGraph(data.graph))
            return [];
        return data.contracts.filter(c => c && text(c.name) && text(c.pre) && text(c.post) && (c.epsilon === null || text(c.epsilon)) && number(c.nodeStart) && number(c.nodeCount) && c.nodeCount > 0 && c.nodeStart + c.nodeCount <= data.graph.nodes.length && ['inputs', 'outputs'].every(side => Array.isArray(c[side]) && c[side].length <= 128 && c[side].every(e => Array.isArray(e) && e.length === 2 && number(e[0]) && number(e[1]) && e[0] >= c.nodeStart && e[0] < c.nodeStart + c.nodeCount && e[1] < data.graph.nodes[e[0]][side])));
    }
    function graphScene(g, contracts, detailId) {
        const widths = g.nodes.map(n => ['identity', 'split', 'swap', 'terminal'].includes(n.kind) ? 36 : ['add', 'multiply'].includes(n.kind) ? 46 : Math.max(76, Math.min(220, n.label.length * 8 + 22)));
        const columns = [...new Set(g.nodes.map(n => n.x))].sort((a, b) => a - b);
        const positions = new Map();
        let x = 150;
        for (const column of columns) {
            positions.set(column, x);
            x += Math.max(...g.nodes.filter(n => n.x === column).map(n => widths[g.nodes.indexOf(n)])) + (contracts.length ? 190 : 90);
        }
        const feedback = g.edges.filter(e => e.feedback);
        const top = 60 + feedback.length * 22;
        const nodes = g.nodes.map((n, i) => ({ ...n, x: positions.get(n.x), y: top + n.y * 0.8, w: widths[i], h: 32 * (Math.max(1, n.inputs, n.outputs) + 1) }));
        const point = (e, out) => { const n = nodes[e[0]]; return [n.x + (out ? n.w : 0), n.y + 32 * (e[1] + 1)]; };
        const width = Math.max(430, x + 40), height = Math.max(170, ...nodes.map(n => n.y + n.h + 55));
        if (width > 60000 || height > 30000)
            throw new Error('Diagram exceeds display limits; select a smaller circuit.');
        let html = '';
        for (const [i, e] of g.edges.entries()) {
            const [sx, sy] = point(e.source, true), [tx, ty] = point(e.target, false);
            const active = contracts.some(contract => (contract.outputs.some(p => p[0] === e.source[0] && p[1] === e.source[1]) || contract.inputs.some(p => p[0] === e.target[0] && p[1] === e.target[1])));
            const cls = `circuit-wire${e.feedback ? ' circuit-feedback' : ''}${active ? ' circuit-highlight' : ''}`;
            if (e.feedback) {
                const lane = 30 + feedback.indexOf(e) * 22;
                html += `<path class="${cls}" d="M${sx} ${sy}H${width - 40 - feedback.indexOf(e) * 8}V${lane}H${45 + feedback.indexOf(e) * 8}V${ty}H${tx}"/>`;
            }
            else {
                const middle = (sx + tx) / 2;
                html += `<path class="${cls}" d="M${sx} ${sy}C${middle} ${sy} ${middle} ${ty} ${tx} ${ty}"/>`;
            }
        }
        g.inputs.forEach((e, i) => { const [px, py] = point(e, false); html += line(20, py, px, py) + label(20, py - 9, `in ${i}`, 'circuit-port'); });
        g.outputs.forEach((e, i) => { const [px, py] = point(e, true); html += line(px, py, width - 20, py) + label(width - 20, py - 9, `out ${i}`, 'circuit-port', 'end'); });
        nodes.forEach((n, i) => {
            const selected = contracts.some(contract => i >= contract.nodeStart && i < contract.nodeStart + contract.nodeCount);
            html += `<g class="circuit-node${selected ? ' selected' : ''}"><title>${esc(n.label)} · ${n.inputs} inputs, ${n.outputs} outputs</title>`;
            const cy = n.y + n.h / 2;
            if (n.kind === 'identity')
                html += line(n.x, n.y + 32, n.x + n.w, n.y + 32);
            else if (n.kind === 'split') {
                html += line(n.x, n.y + 32, n.x + n.w, n.y + 32) + line(n.x + n.w / 2, n.y + 32, n.x + n.w / 2, n.y + 64) + line(n.x + n.w / 2, n.y + 64, n.x + n.w, n.y + 64) + `<circle class="circuit-junction" cx="${n.x + n.w / 2}" cy="${n.y + 32}" r="3.5"/>`;
            }
            else if (n.kind === 'swap') {
                html += line(n.x, n.y + 32, n.x + n.w, n.y + 64) + line(n.x, n.y + 64, n.x + n.w, n.y + 32, 'circuit-crossing') + line(n.x, n.y + 64, n.x + n.w, n.y + 32);
            }
            else if (n.kind === 'terminal')
                html += line(n.x, n.y + 32, n.x + n.w / 2, n.y + 32) + line(n.x + n.w / 2, n.y + 24, n.x + n.w / 2, n.y + 40);
            else {
                // Arithmetic uses a compact body; explicit numbered ports retain order.
                html += `<rect class="circuit-operator ${n.kind === 'integrate' ? 'integrator' : ''}" x="${n.x}" y="${n.y + 12}" width="${n.w}" height="${n.h - 24}" rx="${['add', 'multiply'].includes(n.kind) ? 18 : 7}"/>`;
                html += label(n.x + n.w / 2, cy + 5, n.label, 'circuit-operation', 'middle');
                for (let p = 0; p < n.inputs; p++)
                    html += `<circle class="circuit-pin" cx="${n.x}" cy="${n.y + 32 * (p + 1)}" r="2.5"/>`;
                for (let p = 0; p < n.outputs; p++)
                    html += `<circle class="circuit-pin" cx="${n.x + n.w}" cy="${n.y + 32 * (p + 1)}" r="2.5"/>`;
            }
            html += '</g>';
        });
        const seen = new Set(), occupied = new Map();
        for (const contract of contracts) {
            for (const [side, predicate] of [['inputs', contract.pre], ['outputs', contract.post]]) {
                const points = contract[side].map(e => point(e, side === 'outputs'));
                if (!points.length)
                    continue;
                const out = side === 'outputs';
                const wires = contract[side].map(e => {
                    const edge = g.edges.find(w => out ? w.source[0] === e[0] && w.source[1] === e[1] : w.target[0] === e[0] && w.target[1] === e[1]);
                    return edge ? `${edge.source.join(':')}>${edge.target.join(':')}` : `${side}:${e.join(':')}`;
                }).join('|');
                const predicateKey = contract[out ? 'postKey' : 'preKey'];
                const identity = text(predicateKey) && (contract.epsilon === null || text(contract.epsilonKey))
                    ? JSON.stringify([wires, predicateKey, out ? contract.epsilonKey : null])
                    : `${contract.index}:${side}`;
                if (seen.has(identity))
                    continue;
                seen.add(identity);
                const px = (out ? Math.max(...points.map(p => p[0])) : Math.min(...points.map(p => p[0]))) + (out ? 36 : -36);
                const low = Math.min(...points.map(p => p[1])), high = Math.max(...points.map(p => p[1]));
                if (points.length > 1) {
                    html += line(px, low, px, high, 'circuit-boundary');
                    for (const [, ey] of points)
                        html += line(px, ey, px + (out ? -6 : 6), ey, 'circuit-boundary');
                }
                const caption = side === 'outputs' && contract.epsilon !== null ? `${predicate} (ε = ${contract.epsilon})` : predicate;
                const place = `${Math.round(px / 120)}:${low}`;
                const lift = occupied.get(place) || 0;
                occupied.set(place, lift + 20);
                html += `<a class="circuit-condition" data-act="diagram-condition" href="#${detailId}-${contract.index}"><title>${esc(caption)} · ${esc(contract.name)}</title>${label(px, low - 15 - lift, short(caption), '', 'middle')}</a>`;
            }
        }
        return { width, height, html };
    }
    function modelScene(model) {
        if (!model || !text(model.axis) || !text(model.start) || !Array.isArray(model.states) || model.states.length > 32 || !model.states.every(s => s && ['name', 'equation', 'initial', 'id'].every(k => text(s[k]))) || !Array.isArray(model.observations) || model.observations.length > 64 || !model.observations.every(o => Array.isArray(o) && o.length === 2 && o.every(text)))
            throw new Error('Invalid model diagram data');
        const rows = model.states, fieldWidth = Math.max(330, ...rows.map(s => s.equation.length * 7.6 + 36));
        const fieldLeft = Math.max(80, rows.length * 10 + 35);
        const ix = fieldLeft + fieldWidth + 70, iw = Math.max(150, ...rows.map(s => (s.name.length + model.start.length + s.initial.length + 6) * 8 + 30));
        const width = ix + iw + Math.max(170, rows.length * 10 + 70), blockHeight = Math.max(100, rows.length * 80 + 40), height = blockHeight + 130 + rows.length * 24;
        let html = label(35, 26, `${model.axis} · start ${model.start}`, 'circuit-port');
        html += `<rect class="circuit-operator" x="${fieldLeft}" y="50" width="${fieldWidth}" height="${blockHeight}" rx="9"/><rect class="circuit-operator integrator" x="${ix}" y="50" width="${iw}" height="${blockHeight}" rx="9"/>`;
        html += label(fieldLeft + 18, 76, 'Vector field', 'circuit-heading') + label(ix + 18, 76, `Integrate · ${model.axis}`, 'circuit-heading');
        rows.forEach((s, i) => {
            const y = 110 + i * 80, bottom = blockHeight + 85 + i * 24, right = width - 35 - i * 9, left = 24 + i * 9;
            html += label(fieldLeft + 18, y, s.equation, 'circuit-equation') + line(fieldLeft + fieldWidth, y - 5, ix, y - 5) + label(ix + 18, y, `∫ → ${s.name}`, 'circuit-equation') + label(ix + 18, y + 23, `${s.name}(${model.start}) = ${s.initial}`, 'circuit-port');
            html += line(ix + iw, y - 5, width - 15, y - 5) + label(ix + iw + 12, y - 16, s.name, 'circuit-port');
            html += `<path class="circuit-wire circuit-feedback" d="M${right} ${y - 5}V${bottom}H${left}V${y - 5}H${fieldLeft}"/><circle class="circuit-junction feedback" cx="${right}" cy="${y - 5}" r="3"/>` + label(fieldLeft + 10, bottom - 6, s.name, 'circuit-feedback-label');
        });
        return { width, height, html };
    }
    function render(data, { key, state = {}, claimFor = () => null, idSuffix = '' } = {}) {
        const contracts = contractsOf(data).map((c, index) => ({ ...c, index }));
        const selected = Number.isInteger(state.contract) ? Math.min(Math.max(-1, state.contract), contracts.length - 1) : -1;
        const shown = state.labels === false ? [] : selected < 0 ? contracts : [contracts[selected]].filter(Boolean);
        const detailId = `diagram-proof-${(key + idSuffix).replace(/[^a-zA-Z0-9-]/g, '-')}`;
        let scene;
        try {
            if (data.model && state.mode !== 'circuit')
                scene = modelScene(data.model);
            else {
                const graph = data.model ? data.detail : data.graph;
                if (!validGraph(graph))
                    throw new Error('Circuit detail is unavailable; select a smaller circuit.');
                scene = graphScene(graph, shown, detailId);
            }
        }
        catch (error) {
            scene = { error: String(error.message) };
        }
        const control = (command, title, body = title) => `<button type="button" class="secondary" data-act="diagram" data-command="${command}" data-key="${esc(key)}" title="${title}" aria-label="${title}">${body}</button>`;
        const controls = control('out', 'Zoom out', '−') + control('in', 'Zoom in', '+') + control('fit', 'Fit diagram', 'Fit') + control('actual', 'Actual size', '100%') + control('expand', 'Expand diagram', 'Expand');
        const mode = data.model ? control(state.mode === 'circuit' ? 'overview' : 'circuit', state.mode === 'circuit' ? 'Model overview' : 'Underlying circuit') : '';
        const proofControls = contracts.length ? `<label>Hoare triple <select data-diagram-contract="${esc(key)}"><option value="-1" ${selected < 0 ? 'selected' : ''}>All selected triples</option>${contracts.map((c, i) => `<option value="${i}" ${i === selected ? 'selected' : ''}>${esc(c.name)}${contracts.filter(x => x.name === c.name).length > 1 ? ` · component ${c.nodeStart + 1}` : ''}</option>`).join('')}</select></label><button type="button" class="secondary" data-act="diagram" data-command="labels" data-key="${esc(key)}" aria-pressed="${state.labels !== false}">Conditions</button>` : '';
        const zoom = typeof state.zoom === 'number' ? Math.max(0.001, Math.min(3, state.zoom)) : null;
        const dimensions = scene.error ? 0 : Math.round(scene.width * (zoom || 1));
        const proofHTML = shown.map(contract => { const proof = claimFor(contract.name); return `<details class="def-lean diagram-proof" data-fold-key="${detailId}-${contract.index}" id="${detailId}-${contract.index}"><summary>Hoare triple · ${esc(contract.name)}${proof ? ` · ${esc(proof.label)}` : ' · declaration'}</summary><div><b>Precondition</b> <code>${esc(contract.pre)}</code>${text(contract.preDetail) ? `<pre>${esc(contract.preDetail)}</pre>` : ''}</div><div><b>Postcondition</b> <code>${esc(contract.post)}</code>${text(contract.postDetail) ? `<pre>${esc(contract.postDetail)}</pre>` : ''}${contract.epsilon !== null ? ` · distance ≤ <code>${esc(contract.epsilon)}</code>` : ''}</div>${proof ? `<button type="button" class="link" data-act="goto" data-id="${esc(proof.name)}">View theorem</button>` : ''}<p class="small muted">${contract.epsilon !== null ? 'The output is within the stated distance of the postcondition set.' : 'Conditions describe this component’s whole input and output tuples.'}</p></details>`; }).join('');
        const observations = !scene.error && data.model && state.mode !== 'circuit' ? `<div class="diagram-observations"><span class="small muted">Observations</span> ${data.model.observations.map(([name, id]) => `<span title="${esc(id)}">${esc(name)}</span>`).join(' ')}</div>` : '';
        return `<div class="diagram-toolbar" role="group" aria-label="Diagram controls">${controls}${mode}${proofControls}</div>${scene.error ? `<p class="diagram-error">${esc(scene.error)}</p>` : `<div class="diagram-viewport" tabindex="0" role="region" aria-label="Circuit diagram; scroll to explore" data-diagram-key="${esc(key)}"><svg class="circuit-scene${zoom ? '' : ' diagram-fit'}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${scene.width} ${scene.height}" width="${dimensions}" role="img" aria-label="${data.model && state.mode !== 'circuit' ? 'Continuous model overview' : 'Circuit with ordered input and output ports'}"><title>${data.model ? 'Continuous model' : 'Typed circuit'}${shown.length ? ' · Hoare conditions' : ''}</title>${scene.html}</svg></div>`}${observations}${proofHTML}`;
    }
    window.glitnirCircuits = Object.freeze({ render, validGraph, contractsOf });
})();
