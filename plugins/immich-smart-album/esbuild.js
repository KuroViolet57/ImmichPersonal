const esbuild = require('esbuild');

esbuild.build({
  entryPoints: ['src/index.ts'],
  outdir: 'dist',
  bundle: true,
  sourcemap: false,
  minify: false,
  // QuickJS, which extism-js embeds, needs CommonJS and tops out at ES2020.
  format: 'cjs',
  target: ['es2020'],
});
