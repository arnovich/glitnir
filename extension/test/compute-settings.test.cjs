const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');

function load(trusted = true) {
  const exports = {};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../dist/compute-settings'), 'utf8'), {
    exports, URL, require: name => {
      assert.equal(name, 'vscode');
      return {workspace: {isTrusted: trusted}};
    },
  });
  return exports;
}
const config = values => ({get: (key, fallback) => values[key] ?? fallback});
const remote = {executionBackend: 'taskvalet', taskvaletServer: 'https://TASKS.example:443/',
  taskvaletProject: 'lab', taskvaletEnvironment: 'lean-v1'};

test('remote secrets are canonical-origin/project scoped, local clears inherited values', async () => {
  const {executionEnvironment, credentialScope} = load();
  const scope = credentialScope(config(remote));
  assert.equal(scope.server, 'https://tasks.example');
  assert.notEqual(scope.key, credentialScope(config({...remote, taskvaletProject: 'other'})).key);
  const env = await executionEnvironment(config(remote), {get: async key => {
    assert.equal(key, scope.key);
    return 'private-token';
  }});
  assert.equal(env.GLITNIR_TASKVALET_TOKEN, 'private-token');
  assert.equal(env.GLITNIR_EXECUTION_BACKEND, 'taskvalet');
  const local = await executionEnvironment(config({}), {get: () => {throw Error('unexpected secret read');}});
  assert.equal(local.GLITNIR_TASKVALET_TOKEN, '');
  assert.equal(local.GLITNIR_EXECUTION_BACKEND, 'local');
});

test('untrusted or invalid destinations cannot read credentials', async () => {
  const secrets = {get: () => {throw Error('unexpected secret read');}};
  await assert.rejects(load(false).executionEnvironment(config(remote), secrets), /Trust/);
  for (const server of ['http://tasks.example', 'https://user:pass@tasks.example', 'https://tasks.example/path']) {
    await assert.rejects(load().executionEnvironment(config({...remote, taskvaletServer: server}), secrets), /HTTPS/);
  }
});

test('manifest exposes remote options without a secret setting', () => {
  const properties = require('../../package.json').contributes.configuration.properties;
  assert.deepEqual(properties['glitnir.executionBackend'].enum, ['local', 'taskvalet']);
  for (const name of ['taskvaletServer', 'taskvaletProject', 'taskvaletEnvironment']) assert.ok(properties['glitnir.' + name]);
  assert.ok(!Object.keys(properties).some(name => /token|credential|password/i.test(name)));
});
