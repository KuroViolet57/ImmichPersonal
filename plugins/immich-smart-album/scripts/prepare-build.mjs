/**
 * Generates dist/index.d.ts, which extism-js needs in order to know which
 * functions to export from the WASM module and which host functions to import.
 *
 * This mirrors `plugin-sdk prepareBuild` from the Immich monorepo. It is
 * reimplemented here because @immich/plugin-sdk is not published to npm.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

// Must match `availableFunctions` in the server's plugin host.
const HOST_FUNCTIONS = [
  'searchAlbums',
  'createAlbum',
  'addAssetsToAlbum',
  'addAssetsToAlbums',
  'httpRequest',
  'bulkTagAssets',
];

const manifest = JSON.parse(readFileSync('manifest.json', 'utf8'));
const methods = manifest.methods.map((method) => method.name);

mkdirSync('dist', { recursive: true });
writeFileSync(
  'dist/index.d.ts',
  `declare module 'extism:host' {
  interface user {
${HOST_FUNCTIONS.map((name) => `    ${name}(ptr: PTR): I64;`).join('\n')}
  }
}

declare module 'main' {
${methods.map((name) => `  export function ${name}(): I32;`).join('\n')}
}
`,
);

console.log(`prepare-build: declared ${methods.length} method(s): ${methods.join(', ')}`);
