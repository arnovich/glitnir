const assert = require('node:assert/strict');
const {test} = require('node:test');
const Module = require('node:module');
const path = require('node:path');
test('explicit uv setup shares a validated backend and respects dismissal/trust', async () => {
  const file = fsPath => ({fsPath,scheme:'file',toString:()=>`file://${fsPath}`});
  const global = new Map(), workspace = new Map(), settings = [], dialogs = [];
  let installCalls=0, action='Set Up', installed=false;
  const fake={Uri:{joinPath:(uri,...parts)=>file(path.join(uri.fsPath,...parts))},extensions:{getExtension:()=>undefined},env:{},ConfigurationTarget:{WorkspaceFolder:3},
    workspace:{isTrusted:true,workspaceFolders:[],getWorkspaceFolder:()=>({uri:file('/work')}),getConfiguration:()=>({get:()=>'',update:async(...args)=>settings.push(args)})},
    window:{showInformationMessage:async()=>action,showQuickPick:async()=>({action:'download'}),showOpenDialog:async()=>dialogs.shift()},commands:{executeCommand:async()=>{}}};
  const original=Module._load; Module._load=function(name,...args){return name==='vscode'?fake:original.call(this,name,...args);};
  let EnvironmentSetup; try {({EnvironmentSetup}=require('../../dist/setup'));} finally{Module._load=original;}
  const d={asset:'glitnir-0.1.0-py3-none-any.whl',size:1,sha256:'a'.repeat(64)};
  const state=map=>({get:key=>map.get(key),update:async(k,v)=>map.set(k,v)});
  const setup=new EnvironmentSetup({extensionUri:file('/extension'),extension:{packageJSON:{glitnirBackend:d}},workspaceState:state(workspace),globalState:state(global)},{append(){},appendLine(){}});
  setup.probe=async(...args)=>{assert.equal(args.length,1);if(!installed || args[0]!=='/shared/bin/python')throw Error('missing');return {python:args[0]};};
  setup.installWithUv=async()=>{installCalls++;installed=true;return '/shared/bin/python';};
  action=undefined; assert.equal(await setup.resolve(file('/work/A.lean'),true),undefined); assert.equal(installCalls,0);
  action='Set Up';
  assert.equal((await setup.resolve(file('/work/A.lean'),true)).python,'/shared/bin/python');
  assert.equal(global.get(`backend:${d.sha256}`),'/shared/bin/python'); assert.equal(settings.length,0);
  fake.extensions.getExtension=()=>({activate:async()=>({environments:{getActiveEnvironmentPath:()=>({path:'/unrelated'}),resolveEnvironment:async()=>({executable:{uri:file('/unrelated/python')}})}})});
  workspace.clear(); assert.equal((await setup.resolve(file('/another/A.lean'))).python,'/shared/bin/python'); assert.equal(installCalls,1);
  fake.workspace.isTrusted=false; const before=installCalls;
  await assert.rejects(setup.resolve(file('/work/A.lean'),true),/Trust/); assert.equal(installCalls,before);
});
