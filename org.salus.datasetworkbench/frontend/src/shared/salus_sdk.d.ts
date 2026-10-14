export declare class SalusError extends Error {
    constructor(message: any);
}
/** A frame violates the wire format. */
export declare class ProtocolError extends SalusError {
}
/** A payload could not be parsed or built. */
export declare class PayloadError extends SalusError {
}
/** The framework did not answer a request in time. */
export declare class SalusTimeout extends SalusError {
}
/** An HTTP call to a backend route answered with a non-2xx status (see `Http.json`). */
export declare class HttpError extends SalusError {
    status: any;
    response: any;
    /** The response body as text, if it could be read. */
    body: any;
    constructor(response: any, body: any);
}
export type Sendable = Payload | string | Uint8Array | ArrayBuffer | ArrayBufferView | number[];
/**
 * Immutable message payload with typed accessors.
 *
 * Parse:  asBytes(), asStr(), asJson(), asU8/I8/U16/I16/U32/I32/U64/I64/F32/F64/Bool(),
 *         reader() for compound records.
 * Build:  new Payload(bytes), Payload.str(s), Payload.json(v), Payload.u8/.../f64/bool(v),
 *         Payload.builder() for compound records.
 */
export declare class Payload {
    #private;
    /** @param {Uint8Array | ArrayBuffer | ArrayBufferView | number[]} [data] copied */
    constructor(data?: Uint8Array | ArrayBuffer | ArrayBufferView | number[]);
    static _wrap(bytes: any): Payload;
    /** The raw bytes. Treat as read-only. */
    asBytes(): Uint8Array<ArrayBuffer>;
    asStr(): string;
    asJson(): any;
    reader(): PayloadReader;
    static builder(): PayloadBuilder;
    static str(value: any): Payload;
    static json(value: any): Payload;
    get length(): number;
    equals(other: any): boolean;
    toString(): string;
}
/** Reads a compound record front to back. */
export declare class PayloadReader {
    #private;
    constructor(data: any);
    get remaining(): number;
    get atEnd(): boolean;
    raw(n: any): any;
    rest(): any;
    str(): string;
    bytes(): any;
    json(): any;
    expectEnd(): void;
}
/** Builds a compound record; every method returns the builder for chaining. */
export declare class PayloadBuilder {
    #private;
    raw(data: any): this;
    str(value: any): this;
    bytes(data: any): this;
    json(value: any): this;
    build(): Payload;
}
/**
 * Meta container used by salus:// requests: `u32 meta_len | meta (JSON object) | body`.
 * @param {object} meta
 * @param {Sendable} [body]
 */
export declare function packMeta(meta: object, body?: Sendable): Uint8Array<any>;
/** @returns {{meta: Record<string, any>, body: Uint8Array}} */
export declare function unpackMeta(data: any): {
    meta: Record<string, any>;
    body: Uint8Array;
};
/** A message received on a channel. */
export declare class ChannelMessage {
    /** Channel name without the scheme. */
    channel: any;
    /** @type {Payload} */
    payload: Payload;
    messageId: any;
    /** Frontend id of the sending peer; null for messages from the backend. */
    sender: any;
    constructor(channel: any, payload: any, messageId: any, sender?: null);
}
/**
 * A ws:// channel. Receive with `onMessage(handler)` or by iterating:
 * `for await (const msg of channel) { ... }`. Messages are delivered while a handler is
 * attached or an iterator is active; others are dropped.
 */
export declare class Channel {
    #private;
    /** Channel name without the scheme. */
    name: any;
    /** Full logical channel name on the wire, e.g. `ws://chat`. */
    wireName: any;
    /** Frontend id of the peer for `peer://` channels, otherwise null. */
    peerId: any;
    constructor(salus: any, name: any, wireName: any, handler: any, peerId?: null);
    get closed(): boolean;
    /** @param {Sendable} data */
    send(data: Sendable): void;
    /**
     * Registers `handler(message)`; async handlers are fine, errors are logged and do not
     * affect other messages.
     * @param {(message: ChannelMessage) => void | Promise<void>} handler
     * @returns {() => void} unsubscribe
     */
    onMessage(handler: (message: ChannelMessage) => void | Promise<void>): () => void;
    /** Unregisters the channel and ends all iterators. */
    close(): void;
    [Symbol.asyncIterator](): {
        next: () => Promise<any>;
        return: () => Promise<{
            value: undefined;
            done: boolean;
        }>;
    };
    _deliver(message: any): void;
    _hasReceivers(): boolean;
}
export declare class Peer {
    #private;
    /** Frontend process id of the peer. */
    id: any;
    /** Component name of the peer if known (components of your own plugin; opened components). */
    component: any;
    constructor(salus: any, id: any, component?: null);
    /**
     * Registers the peer channel `name`: messages the peer sends with `peer.send(name, ...)` arrive here
     * (`message.sender` is the peer id). Same interface as `salus.channel()`.
     * @param {string} name
     * @param {(message: ChannelMessage) => void | Promise<void>} [handler]
     */
    channel(name: string, handler?: (message: ChannelMessage) => void | Promise<void>): any;
    /**
     * Sends to the channel `name` of the peer. Messages for channels the peer has not registered are dropped.
     * @param {string} name
     * @param {Sendable} data
     */
    send(name: string, data: Sendable): void;
}
export type HttpOptions = {
    query?: Record<string, string | number | boolean | null | undefined | Array<string | number | boolean>>;
    headers?: Record<string, string>;
    /**
     * raw request body
     */
    body?: BodyInit | Payload | null;
    /**
     * request body encoded as JSON (sets content-type)
     */
    json?: unknown;
    signal?: AbortSignal;
};
/**
 * @typedef {object} HttpOptions
 * @property {Record<string, string | number | boolean | null | undefined | Array<string | number | boolean>>} [query]
 * @property {Record<string, string>} [headers]
 * @property {BodyInit | Payload | null} [body] raw request body
 * @property {unknown} [json] request body encoded as JSON (sets content-type)
 * @property {AbortSignal} [signal]
 */
export declare class Http {
    #private;
    constructor(frontendId: any, { base, fetch: fetchFn }?: {
        base?: string | undefined;
    });
    /**
     * URL of a backend route; use it directly for `<img src>`, `<a href>`, downloads and so on
     * (GET requests carry the session cookie automatically).
     * Encode dynamic path segments yourself with `encodeURIComponent`.
     * @param {string} path starts with "/"
     * @param {HttpOptions["query"]} [query]
     */
    url(path: string, query?: HttpOptions["query"]): string;
    /**
     * Sends a request and returns the standard `Response`. Like `fetch`, error statuses do not throw.
     * @param {string} method
     * @param {string} path
     * @param {HttpOptions} [options]
     * @returns {Promise<Response>}
     */
    request(method: string, path: string, { query, headers, body, json, signal }?: HttpOptions): Promise<Response>;
    get(path: any, options: any): Promise<Response>;
    post(path: any, options: any): Promise<Response>;
    put(path: any, options: any): Promise<Response>;
    patch(path: any, options: any): Promise<Response>;
    delete(path: any, options: any): Promise<Response>;
    /**
     * Sends a request and returns the parsed JSON body (`undefined` for 204 or an empty body).
     * Throws `HttpError` for non-2xx responses.
     * @param {string} method
     * @param {string} path
     * @param {HttpOptions} [options]
     */
    json(method: string, path: string, options?: HttpOptions): Promise<any>;
}
export type SalusOptions = {
    /**
     * defaults to the `fe_process_id` URL parameter Salus adds
     */
    frontendId?: number;
    /**
     * defaults to the `parent_fe_process_id` URL parameter
     */
    parentFrontendId?: number | null;
    /**
     * largest payload per frame; larger messages are fragmented
     */
    maxFragmentPayload?: number;
    /**
     * cap for reassembled incoming messages
     */
    maxMessageSize?: number;
    /**
     * for tests
     */
    window?: Window;
    /**
     * defaults to this page's origin (Salus serves plugins itself)
     */
    parentOrigin?: string;
    /**
     * for tests
     */
    fetch?: typeof fetch;
};
/**
 * @typedef {object} SalusOptions
 * @property {number} [frontendId] defaults to the `fe_process_id` URL parameter Salus adds
 * @property {number | null} [parentFrontendId] defaults to the `parent_fe_process_id` URL parameter
 * @property {number} [maxFragmentPayload] largest payload per frame; larger messages are fragmented
 * @property {number} [maxMessageSize] cap for reassembled incoming messages
 * @property {Window} [window] for tests
 * @property {string} [parentOrigin] defaults to this page's origin (Salus serves plugins itself)
 * @property {typeof fetch} [fetch] for tests
 */
/**
 * Connection of this plugin frontend to the framework (and through it, to the backend).
 */
export declare class Salus {
    #private;
    /** Id of this frontend process; identifies it towards the backend. */
    frontendId: number | undefined;
    /** HTTP access to the routes the backend opened. */
    http: Http;
    /** Name of this frontend's component (from the manifest), or null if unknown. */
    component: string | null;
    /** What this component was opened with (`params` of `openComponent`, or `{file}` for viewers), or null. */
    params: any;
    /** @param {SalusOptions} [options] */
    constructor(options?: SalusOptions);
    /** @param {SalusOptions} [options] */
    static connect(options?: SalusOptions): Salus;
    /**
     * Registers the ws://<name> channel. Throws if it is already registered.
     * @param {string} name
     * @param {(message: ChannelMessage) => void | Promise<void>} [handler]
     */
    channel(name: string, handler?: (message: ChannelMessage) => void | Promise<void>): Channel;
    _peerChannel(peerId: any, name: any, handler: any): Channel;
    /** The frontend that opened this one as a dependency, or null if this frontend was opened by the user. */
    get parent(): any;
    /**
     * Handle for the frontend with the given id; it must be this frontend's parent or one of its
     * dependencies for messages to get through.
     * @param {number} frontendId
     */
    peer(frontendId: number, component?: null): any;
    /**
     * Opens another plugin (an identifier listed under `[dependencies]` in this plugin's manifest) next
     * to this one and resolves with a Peer for its frontend once that has loaded. Without `component`,
     * the plugin's entry component opens; it appears in that component's `target-panel`.
     * @param {string} pluginIdentifier
     * @param {{component?: string, params?: any, reuse?: boolean, timeout?: number}} [options]
     *   `params`: JSON value the component finds in `salus.params`; `reuse`: use the running instance
     *   (and focus its tab) instead of opening another; `timeout`: ms to wait for the framework and the load
     * @returns {Promise<Peer>}
     */
    openDependency(pluginIdentifier: string, { component, params, reuse, timeout }?: {
        component?: string;
        params?: any;
        reuse?: boolean;
        timeout?: number;
    }): Promise<Peer>;
    /**
     * Opens another frontend component of this plugin (names from the manifest's `[[frontend-component]]`)
     * in its `target-panel` and resolves with a Peer for it once it has loaded. No dependency declaration needed.
     * @param {string} componentName
     * @param {{params?: any, reuse?: boolean, timeout?: number}} [options]
     * @returns {Promise<Peer>}
     */
    openComponent(componentName: string, { params, reuse, timeout }?: {
        params?: any;
        reuse?: boolean;
        timeout?: number;
    }): Promise<Peer>;
    /**
     * The other components of this plugin that are currently open (optionally only those named `name`).
     * You can message them like any peer.
     * @param {string} [name]
     * @returns {Promise<Peer[]>}
     */
    components(name?: string): Promise<Peer[]>;
    /**
     * Asks Salus to open a file in a viewer. Salus finds the viewers for the file type and lets the user
     * choose if there are several; you never see which. Rejects with `SalusError` if no viewer exists or the
     * user cancelled. The path is passed on as is, so use what the viewer's backend can open.
     * @param {string} path
     * @returns {Promise<void>}
     */
    openFile(path: string): Promise<void>;
    /**
     * Sends a request to the framework on salus://<topic> and resolves with its reply
     * `{meta, body}`. Rejects with SalusError if the framework refuses and SalusTimeout if it does not answer.
     * @param {string} topic
     * @param {Record<string, any>} [params]
     * @param {{timeout?: number}} [options]
     */
    request(topic: string, params?: Record<string, any>, { timeout }?: {
        timeout?: number;
    }): Promise<any>;
    /**
     * Sends on ws://<name> without registering the channel.
     * @param {string} name
     * @param {Sendable} data
     */
    send(name: string, data: Sendable): void;
    /**
     * Low level: sends on any logical channel name, fragmenting if the message is large.
     * @param {string} logicalChannel full name including the scheme
     * @param {Sendable} data
     */
    sendMessage(logicalChannel: string, data: Sendable): void;
    /**
     * Handler for complete messages on channels that are not registered (including other
     * schemes). Without one, such messages are logged and dropped.
     * @param {(message: {channel: string, payload: Payload, messageId: number}) => void} handler
     * @returns {() => void} unsubscribe
     */
    onUnclaimed(handler: (message: {
        channel: string;
        payload: Payload;
        messageId: number;
    }) => void): () => void;
    /** Stops listening and closes all channels. */
    close(): void;
    _removeChannel(channel: any): void;
}
