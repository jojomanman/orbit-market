// Screenshot the live page with WebGL (SwiftShader). Usage: node tools/shot.js <url> <out.png> [width height wait_ms]
const fs = require('fs');
const { chromium } = require('/opt/agent/browser/node_modules/playwright-core');
const base = require('os').homedir() + '/.cache/ms-playwright';
const dir = fs.readdirSync(base).find((d) => d.startsWith('chromium-'));
const exe = ['chrome-linux64/chrome', 'chrome-linux/chrome'].map((p) => `${base}/${dir}/${p}`).find(fs.existsSync);
(async () => {
  const [url, out, w = 1600, h = 900, wait = 6000] = process.argv.slice(2);
  const b = await chromium.launch({ executablePath: exe, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const p = await b.newPage({ viewport: { width: +w, height: +h } });
  p.on('console', (m) => m.type() === 'error' && console.log('console:', m.text()));
  p.on('pageerror', (e) => console.log('pageerror:', e.message));
  await p.goto(url);
  await p.waitForTimeout(+wait);
  await p.screenshot({ path: out });
  await b.close();
})();
