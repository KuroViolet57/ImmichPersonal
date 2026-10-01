/**
 * Runs the compiled plugin.wasm the way the Immich server does: same Extism
 * options, same host-function wire format, same JSON payload shape.
 *
 * The `httpRequest` host function is backed by a fake Immich whose smart-search
 * results the test controls, so each case pins down one behaviour.
 */
import { readFileSync } from 'node:fs';
import { newPlugin } from '@extism/extism';

const wasm = readFileSync(new URL('../dist/plugin.wasm', import.meta.url));

const ASSET = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const REFERENCE = '33333333-3333-4333-8333-333333333333';

let requests = [];

/** Stands in for Immich: `smart` is the top-N ranking, `indexed` the embeddings. */
let world = { smart: [], indexed: new Set(), status: 200 };

const httpRequest = (plugin, offset) => {
  const { authToken, args } = plugin.read(offset).json();
  if (!authToken) {
    return plugin.store(JSON.stringify({ success: false, status: 400, message: 'no authToken' }));
  }
  const [url, options] = args;
  const body = JSON.parse(options.body);
  requests.push({ url, headers: options.headers, body });

  if (world.status !== 200) {
    return plugin.store(
      JSON.stringify({
        success: true,
        response: { ok: false, status: world.status, body: '{"message":"boom"}' },
      }),
    );
  }

  // queryAssetId against the asset itself is the plugin's "am I indexed?" probe.
  let ids;
  if (body.queryAssetId === ASSET) {
    ids = world.indexed.has(ASSET) ? [OTHER] : [];
  } else if (body.queryAssetId === REFERENCE) {
    ids = world.smart;
  } else {
    ids = world.smart;
  }
  ids = ids.slice(0, body.size ?? 100);

  return plugin.store(
    JSON.stringify({
      success: true,
      response: {
        ok: true,
        status: 200,
        body: JSON.stringify({
          albums: { total: 0, count: 0, items: [], facets: [] },
          assets: {
            total: ids.length,
            count: ids.length,
            items: ids.map((id) => ({ id })),
            facets: [],
            nextPage: null,
          },
        }),
      },
    }),
  );
};

// The module imports every host function the SDK declares, so the host has to
// provide all of them -- the Immich server registers the same six. Only
// httpRequest is exercised here; the rest fail loudly if the plugin calls them.
const unexpected = (name) => (plugin, offset) => {
  void offset;
  return plugin.store(
    JSON.stringify({ success: false, status: 500, message: `unexpected call to ${name}` }),
  );
};

const logs = [];
const plugin = await newPlugin(
  { wasm: [{ data: new Uint8Array(wasm) }] },
  {
    useWasi: true,
    runInWorker: false,
    functions: {
      'extism:host/user': {
        httpRequest,
        searchAlbums: unexpected('searchAlbums'),
        createAlbum: unexpected('createAlbum'),
        addAssetsToAlbum: unexpected('addAssetsToAlbum'),
        addAssetsToAlbums: unexpected('addAssetsToAlbums'),
        bulkTagAssets: unexpected('bulkTagAssets'),
      },
    },
    logger: {
      trace: (m) => logs.push(['trace', m]),
      debug: (m) => logs.push(['debug', m]),
      info: (m) => logs.push(['info', m]),
      log: (m) => logs.push(['info', m]),
      warn: (m) => logs.push(['warn', m]),
      error: (m) => logs.push(['error', m]),
    },
    logLevel: 'debug',
    enableWasiOutput: true,
  },
);

const payload = (config) => ({
  trigger: 'AssetTagged',
  type: 'AssetV1',
  data: {
    asset: {
      id: ASSET,
      ownerId: 'owner',
      type: 'IMAGE',
      originalFileName: 'IMG_0001.jpg',
      localDateTime: '2024-06-01T12:00:00.000Z',
    },
  },
  config,
  workflow: { id: 'wf', authToken: 'token', stepId: 'step' },
});

const call = async (config, setup = {}) => {
  world = { smart: [], indexed: new Set(), status: 200, ...setup };
  requests = [];
  logs.length = 0;
  const result = await plugin.call('smartMatchFilter', JSON.stringify(payload(config)));
  return result.json();
};

let failures = 0;
const check = (name, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}`);
  if (!ok) {
    failures++;
    console.log(`        expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
};

const BASE = { query: 'person in a mountain', apiKey: 'key-123', limit: 50 };

console.log('smartMatchFilter:');

check(
  'asset inside the top N continues',
  await call(BASE, { smart: [OTHER, ASSET], indexed: new Set([ASSET]) }),
  { workflow: { continue: true } },
);

check(
  'asset outside the top N stops',
  await call(BASE, { smart: [OTHER], indexed: new Set([ASSET]) }),
  { workflow: { continue: false } },
);

const notIndexed = await call(BASE, { smart: [OTHER], indexed: new Set() });
check('unindexed asset stops', notIndexed, { workflow: { continue: false } });
check(
  'unindexed asset is diagnosed in the log',
  logs.some(([lvl, m]) => lvl === 'warn' && m.includes('no smart-search embedding yet')),
  true,
);

check(
  'inverse flips a match',
  await call({ ...BASE, inverse: true }, { smart: [ASSET], indexed: new Set([ASSET]) }),
  { workflow: { continue: false } },
);

check(
  'inverse keeps a non-match',
  await call({ ...BASE, inverse: true }, { smart: [OTHER], indexed: new Set([ASSET]) }),
  { workflow: { continue: true } },
);

check(
  'reference photo mode works',
  await call(
    { likeAssetId: REFERENCE, apiKey: 'key-123', limit: 50 },
    { smart: [ASSET], indexed: new Set([ASSET]) },
  ),
  { workflow: { continue: true } },
);

check('missing API key stops', await call({ query: 'x' }), { workflow: { continue: false } });
check('no query and no reference stops', await call({ apiKey: 'k' }), { workflow: { continue: false } });
check(
  'query and reference together stops',
  await call({ query: 'x', likeAssetId: REFERENCE, apiKey: 'k' }),
  { workflow: { continue: false } },
);
check(
  'a server error stops rather than filing',
  await call(BASE, { status: 500 }),
  { workflow: { continue: false } },
);

// Request shape: the plugin must speak the documented smart-search API.
await call(BASE, { smart: [ASSET], indexed: new Set([ASSET]) });
const [first] = requests;
console.log('\nrequest sent to Immich:');
console.log(`  ${first.url}`);
console.log(`  headers: ${JSON.stringify(first.headers)}`);
console.log(`  body:    ${JSON.stringify(first.body)}`);
check('posts to /api/search/smart', first.url, 'http://localhost:2283/api/search/smart');
check('sends the API key header', first.headers['x-api-key'], 'key-123');
check('sends the query', first.body.query, 'person in a mountain');
check('honours the limit', first.body.size, 50);

check(
  'limit is clamped to the API maximum',
  (await call({ ...BASE, limit: 99999 }, { smart: [ASSET], indexed: new Set([ASSET]) }),
  requests[0].body.size),
  1000,
);

await plugin.close();
console.log(failures ? `\n${failures} FAILURE(S)` : '\nALL PLUGIN TESTS PASSED');
process.exit(failures ? 1 : 0);
