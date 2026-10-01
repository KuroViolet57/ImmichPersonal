/**
 * A minimal re-implementation of the parts of `@immich/plugin-sdk` this plugin
 * uses.
 *
 * It is vendored rather than imported because `@immich/plugin-sdk` is a
 * workspace-only package inside the Immich monorepo and is not published to
 * npm, so a standalone plugin cannot depend on it. The wire format below --
 * the `{ authToken, args }` envelope, the `{ success, response }` result, and
 * the `Host.inputString()` / `Host.outputString()` contract -- mirrors the
 * SDK exactly; changing it would break against the host.
 */

export type HttpRequestOptions = {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
};

export type HttpResponse = {
  ok: boolean;
  status: number;
  body: string;
};

type HostFunctionResult<T> =
  | { success: true; response: T }
  | { success: false; status: number; message: string };

/** Asset shape delivered to an `AssetV1` workflow step (trimmed to what we read). */
export type AssetV1 = {
  asset: {
    id: string;
    ownerId: string;
    type: string;
    originalFileName: string;
    localDateTime: string;
  };
};

export type WorkflowEventPayload<TConfig> = {
  trigger: string;
  type: string;
  data: AssetV1;
  config: TConfig;
  workflow: { id: string; authToken: string; stepId: string; debug?: boolean };
};

export type WorkflowResponse = {
  workflow?: { continue?: boolean };
  changes?: Record<string, unknown>;
  data?: Record<string, unknown>;
  config?: Record<string, unknown>;
};

export type HostFunctions = {
  httpRequest: (url: string, options?: HttpRequestOptions) => HttpResponse;
};

const hostFunctions = (authToken: string): HostFunctions => {
  // `dist/index.d.ts`, generated from manifest.json, augments the
  // `extism:host` `user` interface with the host functions the server exposes.
  const host = Host.getFunctions();
  type HostFunctionName = keyof typeof host;

  const call = <T, R>(name: HostFunctionName, args: T): R => {
    const pointer = Memory.fromString(JSON.stringify({ authToken, args }));
    const handler = Memory.find(host[name](pointer.offset));
    const result = JSON.parse(handler.readString()) as HostFunctionResult<R>;

    if (result.success) {
      return result.response;
    }
    throw new Error(
      `Host function "${String(name)}" failed with ${result.status}: ${JSON.stringify(result.message)}`,
    );
  };

  return {
    httpRequest: (url, options) =>
      call<[string, HttpRequestOptions | undefined], HttpResponse>('httpRequest', [url, options]),
  };
};

type Method<TConfig> = (
  payload: WorkflowEventPayload<TConfig> & { functions: HostFunctions },
) => WorkflowResponse | undefined;

/**
 * Wraps plugin methods in the host calling convention: read the JSON payload
 * from Extism memory, run the method, write the JSON response back.
 *
 * A method that throws is logged and rethrown, which the host records as a
 * failed step rather than silently continuing the workflow.
 */
export const wrapper = <T extends Record<string, Method<any>>>(
  methods: T,
): { [K in keyof T]: () => void } => {
  const result = {} as { [K in keyof T]: () => void };

  for (const name of Object.keys(methods) as (keyof T)[]) {
    result[name] = () => {
      try {
        const payload = JSON.parse(Host.inputString()) as WorkflowEventPayload<unknown>;
        const response =
          methods[name]({
            ...payload,
            functions: hostFunctions(payload.workflow.authToken),
          }) ?? {};
        Host.outputString(JSON.stringify(response));
      } catch (error: any) {
        console.error(`[immich-smart-album] ${String(name)} failed: ${error?.message || error}`);
        throw error;
      }
    };
  }

  return result;
};
