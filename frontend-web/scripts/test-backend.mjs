import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const localPython = join(root, '.venv-web', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const python = process.env.EMNETS_UI_PYTHON || (existsSync(localPython) ? localPython : 'python3')
const research = process.argv[2] === 'research'
const args = research
  ? [join(root, 'backend/services/agent_bridge.py'), '--port', '18906', '--agent-dir', join(root, 'backend/agent'),
     '--runs-dir', process.env.EMNETS_UI_RUNS_DIR || join(tmpdir(), 'emnets-ra-web-smoke-data'),
     '--pool-idea', '0', '--pool-exp', '0', '--pool-code', '0', '--pool-exec', '0', '--pool-write', '0', '--no-discussion-mode']
  : [join(root, 'backend/services/review_service.py'), '--host', '127.0.0.1', '--port', '18907']
const child = spawn(python, ['-u', ...args], { stdio: 'inherit' })
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
child.on('error', error => { console.error(error.message); process.exit(1) })
child.on('exit', code => process.exit(code ?? 1))
