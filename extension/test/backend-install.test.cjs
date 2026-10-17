const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const {createHash} = require('node:crypto');
const {backendDescriptor, installBackend, uvEnvironment, findUv} = require('../../dist/backend-install');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'glitnir-uv-'));
  t.after(() => fs.rm(root, {recursive:true, force:true}));
  const wheel = path.join(root, 'glitnir-0.1.0-py3-none-any.whl');
  const bytes = Buffer.from('verified wheel'); await fs.writeFile(wheel, bytes);
  const d = {asset:path.basename(wheel),size:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex'),tag:'v0.1.0'};
  const calls = [];
  const options = {signal:new AbortController().signal, uv:'/installed/uv', wheel,
    run: async (uv, args, cwd) => { calls.push({uv,args,cwd}); if(args[0]==='venv') assert((await fs.stat(args.at(-1))).isDirectory()); },
    probe: async python => ({python})};
  return {root,wheel,d,calls,options,store:path.join(root,'backends')};
}
test('backend descriptor rejects unpinned or unsafe sources', () => {
  const d = {asset:'glitnir-0.1.0-py3-none-any.whl',size:10,sha256:'a'.repeat(64),tag:'build-123'};
  assert.deepEqual(backendDescriptor(d),d);
  assert.deepEqual(backendDescriptor({...d,tag:'v0.1.0'}),{...d,tag:'v0.1.0'});
  for (const tag of ['v01.1.0', 'v1.2', 'v1.2.3/asset', 'v1.2.3\n']) assert.throws(()=>backendDescriptor({...d,tag}));
  for(const invalid of [{asset:'../evil.whl'},{tag:'../../main'},{sha256:'latest'},{size:26*1024**2}]) assert.throws(()=>backendDescriptor({...d,...invalid}));
});
test('uv install verifies bytes first, reuses existing Python and never mutates an existing backend', async t => {
  const f=await fixture(t);
  const python=await installBackend(f.store,f.d,f.options);
  assert.equal(python,path.join(f.store,f.d.sha256,'bin/python'));
  assert.equal(f.calls.length,2);
  assert(f.calls[0].args.includes('--no-python-downloads'));
  assert(f.calls[0].args.includes('>=3.12'));
  assert(f.calls.every(c=>c.args.includes('--no-config') && c.cwd.startsWith(f.store)));
  assert(f.calls[1].args.includes('--no-deps'));
  assert(!f.calls[1].args.some(a=>a.startsWith('lark')));
  assert.equal(await installBackend(f.store,f.d,f.options),python);
  assert.equal(f.calls.length,2,'a completed backend is probed and reused');
});
test('bad wheel, cancellation and failed probes never select or leave partial environments', async t => {
  const f=await fixture(t);
  await fs.writeFile(f.wheel,'bad');
  await assert.rejects(installBackend(f.store,f.d,f.options),/checksum|size/);
  assert.equal(f.calls.length,0);
  await fs.writeFile(f.wheel,'verified wheel');
  const controller=new AbortController(); controller.abort();
  await assert.rejects(installBackend(f.store,f.d,{...f.options,signal:controller.signal}));
  assert.equal(f.calls.length,0);
  await assert.rejects(installBackend(f.store,f.d,{...f.options,probe:async()=>{throw Error('incompatible');}}),/incompatible/);
  await assert.rejects(fs.stat(path.join(f.store,f.d.sha256)),{code:'ENOENT'});
  assert.equal(await installBackend(f.store,f.d,f.options),path.join(f.store,f.d.sha256,'bin/python'));
});
test('unknown directories, locks and symlinked stores are never overwritten', async t => {
  const f=await fixture(t); await fs.mkdir(f.store);
  const target=path.join(f.store,f.d.sha256); await fs.mkdir(target);
  await fs.writeFile(path.join(target,'keep'),'user data');
  await assert.rejects(installBackend(f.store,f.d,f.options),/incomplete|unrecognized/);
  assert.equal(await fs.readFile(path.join(target,'keep'),'utf8'),'user data');
  await fs.mkdir(target+'.lock');
  await assert.rejects(installBackend(f.store,f.d,f.options),/progress/);
  await fs.symlink(f.store,path.join(f.root,'link'));
  await assert.rejects(installBackend(path.join(f.root,'link'),f.d,f.options),/directory/);
});
test('uv subprocess ignores inherited package-manager overrides and unsafe PATH entries', async t => {
  const env=uvEnvironment({PATH:'.:/usr/bin:relative:/tmp/project/.venv/bin',UV_INDEX_URL:'bad',PIP_INDEX_URL:'bad',PYTHONPATH:'bad',VIRTUAL_ENV:'bad',HOME:os.homedir()});
  for(const name of ['UV_INDEX_URL','PIP_INDEX_URL','PYTHONPATH','VIRTUAL_ENV']) assert.equal(env[name],undefined);
  assert(!env.PATH.split(path.delimiter).includes('.'));
  assert(!env.PATH.includes('.venv'));
  const f=await fixture(t); const uv=path.join(f.root,'uv'); await fs.writeFile(uv,'uv'); await fs.chmod(uv,0o700);
  assert.equal(await findUv([uv]),uv);
  await assert.rejects(findUv([path.join(f.root,'missing')]),/Install uv/);
});

test('wheel import binds an open file, limits growth and supports cancellation while copying', async t => {
  const f=await fixture(t);
  const {copyBackendWheel}=require('../../dist/backend-install');
  const original=fs.open;
  let mode='replace';
  const controller=new AbortController();
  fs.open=async(...args)=>{
    const handle=await original(...args);
    if(args[0]!==f.wheel)return handle;
    const stat=handle.stat.bind(handle);
    handle.stat=async()=>{
      const info=await stat();
      if(mode==='replace') {await fs.rename(f.wheel,f.wheel+'.old');await fs.writeFile(f.wheel,'evil replacement');}
      if(mode==='grow') await fs.appendFile(f.wheel,Buffer.alloc(128*1024));
      if(mode==='cancel') {
        const stream=handle.createReadStream.bind(handle);
        handle.createReadStream=(options)=>{const input=stream(options);input.once('data',()=>controller.abort());return input;};
      }
      return info;
    };
    return handle;
  };
  try {
    const output=path.join(f.root,'copied');
    await copyBackendWheel(f.wheel,output,f.d.size,new AbortController().signal);
    assert.equal(await fs.readFile(output,'utf8'),'verified wheel','pathname replacement cannot swap the opened file');
    mode='grow';await fs.writeFile(f.wheel,'verified wheel');
    await assert.rejects(copyBackendWheel(f.wheel,output+'2',f.d.size,new AbortController().signal),/size/);
    assert((await fs.stat(output+'2')).size<=f.d.size);
    mode='cancel';await fs.writeFile(f.wheel,Buffer.alloc(256*1024));
    await assert.rejects(copyBackendWheel(f.wheel,output+'3',256*1024,controller.signal),/abort/i);
  } finally {fs.open=original;}
});

test('uv output streams as it arrives; failures, timeouts and cancellation stop uv', async t => {
  const {runUv} = require('../../dist/backend-install');
  const f = await fixture(t);
  const uv = path.join(f.root, 'fake-uv');
  await fs.writeFile(uv, '#!/bin/sh\necho "Resolved 1 package"\nif [ "$1" = fail ]; then echo "error: no" >&2; exit 2; fi\nsleep "$2"\necho done\n');
  await fs.chmod(uv, 0o700);
  const seen = [];
  await runUv(uv, ['ok', '1'], f.root, new AbortController().signal, {log: text => seen.push(text)});
  assert.match(seen.join(''), /Resolved 1 package[\s\S]*done/);
  seen.length = 0;
  await assert.rejects(runUv(uv, ['fail', '0'], f.root, new AbortController().signal, {log: text => seen.push(text), failure: 'install failed'}), /^Error: install failed$/);
  assert.match(seen.join(''), /error: no/, 'stderr reaches the log');
  await assert.rejects(runUv(uv, ['ok', '30'], f.root, new AbortController().signal, {timeoutMs: 300, failure: 'install failed'}), /install failed uv did not finish within 0 s/);
  const {UvNotStartedError} = require('../../dist/backend-install');
  const missing = runUv(path.join(f.root, 'no-uv'), ['ok', '0'], f.root, new AbortController().signal, {failure: 'install failed'});
  await assert.rejects(missing, error => error instanceof UvNotStartedError && /could not be started at .*no-uv \(ENOENT\)/.test(error.message));
  const controller = new AbortController();
  const cancelled = runUv(uv, ['ok', '30'], f.root, controller.signal, {log: () => controller.abort()});
  await assert.rejects(cancelled, /abort/i);
});
