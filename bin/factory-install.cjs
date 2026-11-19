#!/usr/bin/env node
'use strict';

const { spawnSync } = require('node:child_process');
const { join } = require('node:path');

const script = join(__dirname, '..', 'install.py');
const result = spawnSync('python3', [script, '--managed', ...process.argv.slice(2)], {
  stdio: 'inherit',
});

if (result.error) {
  console.error(result.error.code === 'ENOENT'
    ? 'Factory installation requires Python 3.9 or newer. Install python3 and try again.'
    : `Could not start the Factory installer: ${result.error.message}`);
}
process.exitCode = result.status ?? 1;
