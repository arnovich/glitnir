import * as path from 'node:path';
import * as vscode from 'vscode';
import {installSkill} from './skill-install';

/** Let the owner choose a project-local discovery location or a portable export. */
export async function setupAgent(context: vscode.ExtensionContext): Promise<void> {
  const allowed = (): boolean => vscode.workspace.isTrusted && !vscode.env.remoteName;
  if (!allowed()) throw new Error('Open a trusted local workspace before setting up an agent skill.');
  const folders = vscode.workspace.workspaceFolders ?? [];
  let root: vscode.Uri | undefined;
  if (folders.length === 1) root = folders[0].uri;
  else if (folders.length > 1) {
    root = (await vscode.window.showQuickPick(folders.map(folder => ({label: folder.name, description: folder.uri.fsPath, uri: folder.uri})), {title: 'Choose the project for the Glitnir skill'}))?.uri;
    if (!root) return;
  } else {
    root = (await vscode.window.showOpenDialog({title: 'Choose a project folder for the Glitnir skill', canSelectFolders: true, canSelectFiles: false, canSelectMany: false, openLabel: 'Use This Folder'}))?.[0];
    if (!root) return;
  }
  if (root.scheme !== 'file') throw new Error('Skill export currently supports local folders.');
  const choice = await vscode.window.showQuickPick([
    {label: 'Codex', description: path.join(root.fsPath, '.agents/skills/glitnir-notebooks'), parent: '.agents/skills'},
    {label: 'Claude Code', description: path.join(root.fsPath, '.claude/skills/glitnir-notebooks'), parent: '.claude/skills'},
    {label: 'Export for another agent', description: 'Choose a folder and give its SKILL.md to your agent.', parent: ''},
  ], {title: 'Install Glitnir authoring instructions', placeHolder: 'Choose an agent; its destination is shown below. Existing edits are preserved.'});
  if (!choice) return;
  if (choice.parent === '') {
    root = (await vscode.window.showOpenDialog({title: 'Export a glitnir-notebooks folder here', canSelectFolders: true, canSelectFiles: false, canSelectMany: false, openLabel: 'Export Skill Here'}))?.[0];
    if (!root) return;
    if (root.scheme !== 'file') throw new Error('Skill export currently supports local folders.');
  }
  const result = await installSkill(
    vscode.Uri.joinPath(context.extensionUri, 'skills/glitnir-notebooks').fsPath,
    root.fsPath, choice.parent, context.extension.packageJSON.version, allowed,
  );
  const skill = vscode.Uri.file(path.join(root.fsPath, choice.parent, 'glitnir-notebooks/SKILL.md'));
  const action = await vscode.window.showInformationMessage(
    `Glitnir authoring skill ${result === 'current' ? 'is already current' : result}. ${choice.parent === '' ? `Give ${skill.fsPath} to your agent and ask it to follow those instructions.` : 'Start a new agent session in this project and ask it to use glitnir-notebooks.'}${result === 'updated' ? ` The previous copy is retained in ${path.join(root.fsPath, '.glitnir-skill-backups')}.` : ''}`,
    'Open Instructions',
  );
  if (action === 'Open Instructions') await vscode.window.showTextDocument(skill);
}
