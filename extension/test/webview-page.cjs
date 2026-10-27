// The packaged VS Code page: `previewHtml` run against a fake webview and a fake `vscode` module.
const Module = require('node:module');
const path = require('node:path');

function previewPage(media = path.resolve(__dirname, '../../dist/media')) {
  const file = fsPath => ({fsPath});
  const load = Module._load;
  let previewHtml;
  Module._load = function (name, parent, ...rest) {
    if (name === 'vscode') return {Uri: {joinPath: (uri, ...parts) => file(path.join(uri.fsPath, ...parts))}};
    return load.call(this, name, parent, ...rest);
  };
  try { ({previewHtml} = require('../../dist/webview')); } finally { Module._load = load; }
  return previewHtml({cspSource: 'https://resource.invalid', asWebviewUri: uri => ({toString: () => 'https://resource.invalid/' + path.basename(uri.fsPath)})}, file(media));
}

module.exports = {previewPage};
