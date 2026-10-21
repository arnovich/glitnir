import * as vscode from 'vscode';

/** Settings that change a running backend's destination or resource budget. */
export const executionSettings = [
  'pythonPath', 'executionBackend', 'taskvaletServer', 'taskvaletProject',
  'taskvaletEnvironment', 'elaborationTimeoutMs', 'memoryLimitMb',
];

/** Canonical origin and project bind each secret to its intended recipient. */
export function credentialScope(config: vscode.WorkspaceConfiguration): {key: string; server: string; project: string} {
  const configured = config.get<string>('taskvaletServer', '');
  let url: URL;
  try { url = new URL(configured); } catch { throw new Error('Configure glitnir.taskvaletServer with an HTTPS server origin.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Taskvalet server must be an HTTPS origin without credentials or a path.');
  }
  const project = config.get<string>('taskvaletProject', '');
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(project)) throw new Error('Configure glitnir.taskvaletProject.');
  return {key: 'glitnir.taskvalet:' + JSON.stringify([url.origin, project]), server: url.origin, project};
}

/** Pass credentials only to the trusted backend, never a command line or webview. */
export async function executionEnvironment(config: vscode.WorkspaceConfiguration, secrets: vscode.SecretStorage): Promise<NodeJS.ProcessEnv> {
  const mode = config.get<string>('executionBackend', 'local');
  // Explicit empty values also erase inherited remote credentials in local mode.
  const env: NodeJS.ProcessEnv = {
    GLITNIR_EXECUTION_BACKEND: mode, GLITNIR_TASKVALET_URL: '',
    GLITNIR_TASKVALET_PROJECT: '', GLITNIR_TASKVALET_ENVIRONMENT: '',
    GLITNIR_TASKVALET_TOKEN: '', GLITNIR_TASKVALET_CA_FILE: '',
  };
  if (mode === 'local') return env;
  if (mode !== 'taskvalet') throw new Error('Choose local or taskvalet as the Glitnir execution backend.');
  if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before using Taskvalet credentials.');
  const scope = credentialScope(config);
  const environment = config.get<string>('taskvaletEnvironment', '');
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(environment)) throw new Error('Configure glitnir.taskvaletEnvironment.');
  const token = await secrets.get(scope.key);
  if (!token || token.length > 256 || /\s/.test(token)) throw new Error('Run Glitnir: Set Taskvalet Credential for this server and project.');
  return {...env, GLITNIR_TASKVALET_URL: scope.server, GLITNIR_TASKVALET_PROJECT: scope.project,
    GLITNIR_TASKVALET_ENVIRONMENT: environment, GLITNIR_TASKVALET_TOKEN: token};
}
