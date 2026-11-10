const assert = require('node:assert/strict');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const vm = require('node:vm');
const fs = require('node:fs');
function load() {
 const exports={};vm.runInNewContext(fs.readFileSync(require.resolve('../../dist/handoff-panel'),'utf8'),{exports,require:n=>n==='vscode'?{}:require(n)});return exports;
}
test('review panel renders source as text and submits only the reviewed objective',()=>{
 const messages=[];const dom=new JSDOM(load().handoffHtml(),{runScripts:'dangerously',beforeParse(w){w.acquireVsCodeApi=()=>({postMessage:m=>messages.push(m)});}});
 const w=dom.window;const state={phase:'review',notebook:'Demo.lean',source:'</pre><script>window.attacked=true</script>',objective:'',server:'https://tasks.example',project:'lab'};
 w.dispatchEvent(new w.MessageEvent('message',{data:{type:'state',state}}));
 assert.equal(w.attacked,undefined);assert.equal(w.document.querySelector('#source').textContent,state.source);
 assert.equal(w.document.querySelector('#submit').disabled,true);
 const objective=w.document.querySelector('#objective');objective.value='Simplify the proof';objective.dispatchEvent(new w.Event('input'));
 w.document.querySelector('#submit').click();w.document.querySelector('#submit').click();
 assert.equal(messages.filter(m=>m.action==='submit').length,1);assert.equal(messages.at(-1).objective,'Simplify the proof');
 assert.ok(!dom.serialize().includes('credential_id'));
 dom.window.close();
});
test('submitted panel freezes the objective and displays request status without launch controls',()=>{
 const messages=[];const dom=new JSDOM(load().handoffHtml(),{runScripts:'dangerously',beforeParse(w){w.acquireVsCodeApi=()=>({postMessage:m=>messages.push(m)});}});
 dom.window.dispatchEvent(new dom.window.MessageEvent('message',{data:{type:'state',state:{phase:'submitted',canRefresh:true,stage:'needs_input',objective:'Explore',task:'001-request',server:'https://tasks.example',project:'lab'}}}));
 const d=dom.window.document;assert.equal(d.querySelector('#objective').readOnly,true);assert.equal(d.querySelector('#submit').hidden,true);assert.equal(d.querySelector('#stage').textContent,'Needs input');
 d.querySelector('#refresh').click();assert.equal(messages.at(-1).action,'refresh');assert.equal(d.querySelectorAll('[id*=launch]').length,0);
 dom.window.close();
});

test('providers without a status callback retain the board link without a broken refresh button',()=>{
 const dom=new JSDOM(load().handoffHtml(),{runScripts:'dangerously',beforeParse(w){w.acquireVsCodeApi=()=>({postMessage(){}});}});
 dom.window.dispatchEvent(new dom.window.MessageEvent('message',{data:{type:'state',state:{phase:'submitted',canRefresh:false,stage:'new',objective:'Explore',task:'001-request'}}}));
 const d=dom.window.document;assert.equal(d.querySelector('#refresh').hidden,true);assert.equal(d.querySelector('#open').hidden,false);assert.match(d.querySelector('#status-note').textContent,/Live refresh is unavailable/);
 dom.window.close();
});
