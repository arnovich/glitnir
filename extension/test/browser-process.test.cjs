const assert = require('node:assert/strict');
const {PassThrough} = require('node:stream');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {waitForBrowserShutdown} = require('./browser-process.cjs');

test('a non-running browser releases its stderr pipe before waiting for close', async () => {
  const stderr = new PassThrough();
  const closed = new Promise(resolve => stderr.once('close', resolve));
  const child = {stderr};
  try {
    assert.equal(await waitForBrowserShutdown(child, closed, 50), true);
    assert.equal(stderr.destroyed, true);
  } finally { stderr.destroy(); }
});

test('a closed pipe cannot conceal a browser that is still running', async () => {
  const module = {exports: {}};
  vm.runInNewContext(readFileSync(require.resolve('./browser-process.cjs'), 'utf8'), {
    module, process: {platform: 'darwin'}, setTimeout, clearTimeout,
  });
  const stderr = new PassThrough();
  const child = {pid: 42, exitCode: null, signalCode: null, stderr};
  try {
    assert.equal(await module.exports.waitForBrowserShutdown(child, Promise.resolve(), 25), false);
    assert.equal(stderr.destroyed, false);
  } finally { stderr.destroy(); }
});
