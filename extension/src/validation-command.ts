import {Environment} from './discovery';

const quote = (value: string): string => `'${value.replace(/'/g, `'"'"'`)}'`;

/** Emit a local POSIX command with only the environment needed for checking. */
export function validationCommand(environment: Environment, file: string): string {
  const inherited = environment.processEnv ?? process.env;
  const variables = ['HOME', 'TMPDIR', 'PATH', 'ELAN_HOME', 'PYTHONDONTWRITEBYTECODE'].flatMap(key => inherited[key] ? [`${key}=${inherited[key]}`] : []);
  return ['env', '-i', ...variables, environment.python, '-I', '-B', '-m', 'glitnir.notebook', 'check', file].map(quote).join(' ');
}
