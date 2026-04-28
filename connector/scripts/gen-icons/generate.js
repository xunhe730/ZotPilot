#!/usr/bin/env node
/**
 * ZotPilot icon generator
 * Uses @resvg/resvg-js (WASM) to rasterize SVG → PNG at all required sizes.
 *
 * Usage:
 *   cd scripts/gen-icons
 *   npm install
 *   node generate.js
 */

const { Resvg } = require('@resvg/resvg-js');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');

// Extension icons: source → output directory, sizes
// Small sizes use a filter-free SVG to stay crisp at low resolution
const EXTENSION_ICONS = [
  { size: 16,  out: 'Icon-16.png',  svg: 'zotpilot-icon-small.svg' },
  { size: 32,  out: 'Icon-32.png',  svg: 'zotpilot-icon-32.svg' },
  { size: 48,  out: 'Icon-48.png' },
  { size: 64,  out: 'Icon-64.png' },
  { size: 96,  out: 'Icon-96.png' },
  { size: 128, out: 'Icon-128.png' },
];

// Action button icons: source → output directory, sizes
const ACTION_ICONS = [
  { size: 16, out: 'treeitem-webpage-gray.png' },
  { size: 32, out: 'treeitem-webpage-gray@2x.png' },
  { size: 48, out: 'treeitem-webpage-gray@48px.png' },
];

function renderSvg(svgPath, size) {
  const svg = fs.readFileSync(svgPath, 'utf8');
  const resvg = new Resvg(svg, {
    fitTo: { mode: 'width', value: size },
    font: { loadSystemFonts: false },
  });
  return resvg.render().asPng();
}

function generate(defaultSvgFile, specs, outputDir) {
  fs.mkdirSync(outputDir, { recursive: true });

  for (const { size, out, svg } of specs) {
    const svgPath = path.join(__dirname, svg || defaultSvgFile);
    const outPath = path.join(outputDir, out);
    const png = renderSvg(svgPath, size);
    fs.writeFileSync(outPath, png);
    console.log(`  ✓ ${out} (${size}×${size}) → ${path.relative(ROOT, outPath)}`);
  }
}

console.log('\n🎨 ZotPilot Icon Generator\n');

console.log('Extension icons (icons/):');
generate('zotpilot-icon.svg', EXTENSION_ICONS, path.join(ROOT, 'icons'));

console.log('\nAction button icons (src/browserExt/images/):');
generate('zotpilot-action.svg', ACTION_ICONS, path.join(ROOT, 'src/browserExt/images'));

console.log('\n✅ Done. Run ./build.sh -p b to verify in extension.\n');
