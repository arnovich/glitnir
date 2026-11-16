// Browser teardown is shared with focused lifecycle tests.
const useProcessGroup = process.platform === 'linux';

function within(promise, timeoutMs) {
  let timer;
  return Promise.race([
    promise.then(() => true, () => false),
    new Promise(resolve => { timer = setTimeout(() => resolve(false), timeoutMs); }),
  ]).finally(() => clearTimeout(timer));
}

function browserAlive(child) {
  if (!Number.isInteger(child.pid)) return false;
  if (!useProcessGroup) return child.exitCode === null && child.signalCode === null;
  try {
    process.kill(-child.pid, 0);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    throw error;
  }
}

function signalBrowser(child, signal) {
  if (!browserAlive(child)) return;
  try {
    if (!useProcessGroup) child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
}

async function waitForBrowserShutdown(child, closed, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (browserAlive(child) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  if (browserAlive(child)) return false;
  // A helper can inherit Chrome's stderr after the browser itself exits.
  // Node waits for that pipe before emitting `close`; release our read end
  // only after confirming the browser (or Linux process group) is gone.
  child.stderr?.destroy();
  return within(closed, Math.max(0, deadline - Date.now()));
}

async function stopBrowser(child, closed, client) {
  if (client) await within(client.send('Browser.close'), 1000);
  client?.socket.close();
  if (!Number.isInteger(child.pid)) return;
  if (await waitForBrowserShutdown(child, closed, 2000)) return;
  signalBrowser(child, 'SIGTERM');
  if (await waitForBrowserShutdown(child, closed, 2000)) return;
  signalBrowser(child, 'SIGKILL');
  if (await waitForBrowserShutdown(child, closed, 2000)) return;
  throw new Error(`Chrome process group ${child.pid} did not close after SIGKILL`);
}

module.exports = {stopBrowser, useProcessGroup, waitForBrowserShutdown};
