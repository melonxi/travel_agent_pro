import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
const { build } = require('esbuild')
const scriptDir = path.dirname(fileURLToPath(import.meta.url))

async function loadPhaseIndicatorModule() {
  const entryPoint = path.resolve(scriptDir, '../src/components/PhaseIndicator.tsx')
  const bundlePath = path.resolve(scriptDir, '.phase-indicator.bundle.mjs')
  const result = await build({
    entryPoints: [entryPoint],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: bundlePath,
    jsx: 'automatic',
    external: ['react', 'react-dom/server'],
  })

  assert.ok(result.errors.length === 0, 'Expected esbuild bundling to succeed')

  try {
    return await import(pathToFileURL(bundlePath).href)
  } finally {
    await fs.unlink(bundlePath).catch(() => {})
  }
}

const phaseIndicatorModule = await loadPhaseIndicatorModule()

assert.equal(phaseIndicatorModule.resolveEffectivePhase(1, 3), 3)
assert.equal(phaseIndicatorModule.resolveEffectivePhase(1, null), 1)
assert.equal(phaseIndicatorModule.shouldAnimateAdvance(null, 1), false)
assert.equal(phaseIndicatorModule.shouldAnimateAdvance(1, 3), true)
assert.equal(phaseIndicatorModule.shouldAnimateAdvance(3, 3), false)

const html = renderToStaticMarkup(
  React.createElement(phaseIndicatorModule.default, {
    currentPhase: 1,
    overridePhase: 3,
  }),
)

assert.match(html, /phase-node completed[^"]*[\s\S]*灵感与目的地/)
assert.match(html, /phase-node active[^"]*[\s\S]*日期与住宿/)

console.log('phase-indicator tests passed')
