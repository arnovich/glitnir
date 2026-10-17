// Obtain the canonical renderer from an installed, versioned Glitnir backend.
const {copyFileSync, cpSync, rmSync} = require('node:fs');
const {resolve} = require('node:path');
const {execFileSync} = require('node:child_process');
const target = resolve(__dirname, '../../dist/media');
rmSync(target, {recursive: true, force: true});
const python = process.env.GLITNIR_BUILD_PYTHON || 'python3';
execFileSync(python, ['-I', '-m', 'glitnir.notebook.editor', '--export', target], {stdio: 'inherit'});
copyFileSync(resolve(target, 'editor-runtime.json'), resolve(__dirname, '../../dist/editor-runtime.json'));
copyFileSync(resolve(__dirname, 'probe.py'), resolve(__dirname, '../../dist/probe.py'));
copyFileSync(resolve(__dirname, 'providers.py'), resolve(__dirname, '../../dist/providers.py'));
cpSync(resolve(__dirname, '../media'), target, {recursive: true});
