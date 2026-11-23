import esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';

const uiDir = path.resolve(import.meta.dirname);
const result = await esbuild.build({
  entryPoints: [path.join(uiDir, 'src', 'app.ts')],
  bundle: true,
  minify: true,
  write: false,
  format: 'iife',
  target: 'es2022',
});

const jsCode = result.outputFiles[0].text;
const css = fs.readFileSync(path.join(uiDir, 'src', 'styles.css'), 'utf-8');

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Cowork</title>
<style>${css}</style>
</head>
<body>
<div id="app"></div>
<script>${jsCode}</script>
</body>
</html>
`;

fs.writeFileSync(path.join(uiDir, 'cowork.html'), html, 'utf-8');
console.log(`Built ${path.join(uiDir, 'cowork.html')} (${Buffer.byteLength(html, 'utf-8')} bytes)`);
