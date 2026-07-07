import { spawn, type ChildProcess } from 'node:child_process';
import { logger } from './core/logger.ts';

const log = logger.child({ mod: 'start' });
const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const children = new Map<string, ChildProcess>();
let stopping = false;

function startChild(name: string, command: string, args: string[]): void {
  const child = spawn(command, args, {
    env: process.env,
    stdio: 'inherit',
  });

  children.set(name, child);
  log.info('process started', { name });

  child.on('error', (err) => {
    log.error('process failed to start', { name, err });
    stopAll(1);
  });

  child.on('exit', (code, signal) => {
    children.delete(name);
    if (stopping) return;

    log.error('process exited', { name, code, signal });
    stopAll(code ?? 1);
  });
}

function stopAll(code: number): void {
  if (stopping) return;
  stopping = true;

  const running = [...children.entries()];
  if (running.length === 0) {
    process.exit(code);
  }

  let pending = running.length;
  const done = () => {
    pending -= 1;
    if (pending === 0) process.exit(code);
  };

  for (const [name, child] of running) {
    log.info('stopping process', { name });
    child.once('exit', done);
    child.kill('SIGTERM');
  }

  setTimeout(() => {
    for (const child of children.values()) child.kill('SIGKILL');
    process.exit(code);
  }, 5_000).unref();
}

process.once('SIGINT', () => stopAll(0));
process.once('SIGTERM', () => stopAll(0));

startChild('bot', process.execPath, ['--experimental-strip-types', 'src/index.ts']);
startChild('dashboard-preview', npmCmd, ['run', 'dashboard:preview']);
