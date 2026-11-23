// The page's own tests, then the server's (InfiniEvolve's integration tests run where it is installed).
import { execFileSync } from 'node:child_process';
const run = (cmd, args) => execFileSync(cmd, args, { stdio: 'inherit' });
run(process.execPath, ['--no-warnings', 'viewer/scripts/test-campaign.mjs']);
run(process.execPath, ['viewer/scripts/test-i18n.mjs']);
run(process.env.PYTHON || 'python3', ['-m', 'pytest', '-q', 'tests']);
