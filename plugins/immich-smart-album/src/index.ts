/**
 * Smart Album -- an Immich workflow filter that matches on image content.
 *
 * Immich's core plugin ships filters for filename, date, location, EXIF, tags
 * and asset type: all metadata. None of them can express "photos that look
 * like a mountain". This filter fills that gap by asking Immich's own smart
 * search (the CLIP endpoint behind the web UI's search bar) whether the asset
 * flowing through the workflow is among the closest matches for a description
 * or for a reference photo.
 *
 * Two constraints shape the implementation:
 *
 *  1. Smart search returns no similarity score -- only a ranking. So the test
 *     is "is this asset inside the top N results", and `limit` is the
 *     precision control, exactly as in the immich-organizer CLI.
 *
 *  2. A freshly uploaded asset has no CLIP embedding yet. Immich queues the
 *     SmartSearch job only after thumbnail generation, whereas the
 *     AssetCreate and AssetMetadataExtraction triggers both fire earlier, so
 *     an unindexed asset cannot match anything. Rather than silently dropping
 *     it, the filter diagnoses that case and says so in the workflow log.
 */

import { wrapper, type HostFunctions, type WorkflowResponse } from './runtime.js';

type SmartMatchConfig = {
  query?: string;
  likeAssetId?: string;
  limit?: number;
  inverse?: boolean;
  serverUrl?: string;
  apiKey: string;
  explainMisses?: boolean;
};

const DEFAULT_LIMIT = 200;
const MAX_LIMIT = 1000; // Immich rejects a larger `size`
const DEFAULT_SERVER = 'http://localhost:2283';

const stop: WorkflowResponse = { workflow: { continue: false } };

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, Math.floor(value)));

const searchSmart = (
  functions: HostFunctions,
  config: SmartMatchConfig,
  body: Record<string, unknown>,
) => {
  const base = (config.serverUrl || DEFAULT_SERVER).replace(/\/+$/, '');
  return functions.httpRequest(`${base}/api/search/smart`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'x-api-key': config.apiKey,
    },
    body: JSON.stringify(body),
  });
};

const idsFrom = (response: { body: string }): string[] => {
  const parsed = JSON.parse(response.body) as {
    assets?: { items?: { id?: string }[] };
  };
  const items = (parsed.assets && parsed.assets.items) || [];
  const ids: string[] = [];
  for (const item of items) {
    if (item && item.id) {
      ids.push(item.id);
    }
  }
  return ids;
};

/**
 * Has Smart Search indexed this asset yet?
 *
 * Searching with `queryAssetId` set to the asset itself requires the server to
 * look up that asset's own embedding, so a failure or empty result is a
 * reliable signal that the embedding does not exist.
 */
const isIndexed = (functions: HostFunctions, config: SmartMatchConfig, assetId: string) => {
  try {
    const probe = searchSmart(functions, config, { queryAssetId: assetId, size: 1 });
    return probe.status >= 200 && probe.status < 300 && idsFrom(probe).length > 0;
  } catch {
    return false;
  }
};

const methods = wrapper({
  smartMatchFilter: ({ data, config, functions }): WorkflowResponse => {
    const settings = config as SmartMatchConfig;
    const assetId = data.asset.id;

    if (!settings.apiKey) {
      console.error('[immich-smart-album] no API key configured; the filter cannot search');
      return stop;
    }

    const query = (settings.query || '').trim();
    const likeAssetId = (settings.likeAssetId || '').trim();
    if (!query && !likeAssetId) {
      console.error('[immich-smart-album] set either a description or a reference photo ID');
      return stop;
    }
    if (query && likeAssetId) {
      console.error('[immich-smart-album] set a description OR a reference photo ID, not both');
      return stop;
    }

    const limit = clamp(settings.limit || DEFAULT_LIMIT, 1, MAX_LIMIT);
    const body: Record<string, unknown> = { size: limit, page: 1, withExif: false };
    if (likeAssetId) {
      body.queryAssetId = likeAssetId;
    } else {
      body.query = query;
    }

    const response = searchSmart(functions, settings, body);
    if (response.status < 200 || response.status >= 300) {
      console.error(
        `[immich-smart-album] search failed with ${response.status}: ${response.body.slice(0, 200)}`,
      );
      return stop;
    }

    const matched = idsFrom(response).indexOf(assetId) !== -1;

    // A non-match is ambiguous: the photo may genuinely not match, or it may
    // simply not be indexed yet. Saying which turns a baffling empty album
    // into an obvious fix.
    if (!matched && settings.explainMisses !== false && !isIndexed(functions, settings, assetId)) {
      console.warn(
        `[immich-smart-album] ${data.asset.originalFileName} has no smart-search embedding yet, ` +
          'so it cannot match. Newly uploaded photos are indexed after thumbnail generation; ' +
          'use the AssetTagged trigger, or sweep the library later with the immich-organizer CLI.',
      );
      return stop;
    }

    const keep = settings.inverse ? !matched : matched;
    console.debug(
      `[immich-smart-album] ${data.asset.originalFileName}: ` +
        `${matched ? 'in' : 'not in'} top ${limit} -> ${keep ? 'continue' : 'stop'}`,
    );
    return { workflow: { continue: keep } };
  },
});

const { smartMatchFilter } = methods;

export { smartMatchFilter };
