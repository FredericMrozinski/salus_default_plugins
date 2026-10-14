// Salus plugin frontend SDK. A single ES module without dependencies; copy it into your
// plugin's frontend folder and `import { Salus } from "./salus_sdk.js"`.
//
//   const salus = Salus.connect();
//
//   const chat = salus.channel("chat");                      // ws://chat
//   chat.onMessage((msg) => console.log(msg.payload.asStr()));
//   chat.send("hello backend");
//
//   const doc = await salus.http.json("GET", "/doc/42");     // routes the backend opened
//
//   const editor = await salus.openComponent("editor");     // another frontend component of your plugin
//   const other = await salus.openDependency("org.example.other");  // a plugin from your manifest's [dependencies]
//   editor.send("show", Payload.json({ id: 7 }));            // ...and talk to it
//   await salus.openFile("/data/slide.svs");                 // Salus picks a viewer for the file
//
// Counterpart of the Python backend SDK (salus_sdk.py): same Payload helpers, same channel
// model, same wire semantics (fragmentation, ws:// prefix).
//
// How it reaches the backend: the plugin runs in an iframe of the Salus frontend. Socket
// messages travel as postMessage frames to the parent, which relays them over a websocket to
// the framework and on to the backend. HTTP calls go straight to the framework, which
// forwards them to the backend routes (/plugin-api/<frontend id>/...).

const MAX_ID = 0xFFFFFFFF;
const MAX_FRAME_SIZE = 64 * 1024 * 1024;
const FRAME_HEADER_LEN = 11;
const MAX_CHANNEL_LEN = 0xFFFF;
const FLAG_MORE_FRAGMENTS = 0x01;
const WS_PREFIX = "ws://";
const SALUS_PREFIX = "salus://";
const PEER_PREFIX = "peer://";
// Sent by a dependency to its parent once it has loaded, so `openDependency` can resolve only
// when messages to the dependency will actually arrive.
const READY_CHANNEL = "salus.ready";
const DEFAULT_REQUEST_TIMEOUT = 15_000;

// Largest payload sent in a single frame. Bigger messages are fragmented. The limit is far
// below the 64 MiB wire maximum because every byte crosses postMessage and the websocket
// bridge as a number array.
const DEFAULT_MAX_FRAGMENT_PAYLOAD = 4 * 1024 * 1024;
const DEFAULT_MAX_MESSAGE_SIZE = 256 * 1024 * 1024;

export class SalusError extends Error {
  constructor(message) {
    super(message);
    this.name = new.target.name;
  }
}

/** A frame violates the wire format. */
export class ProtocolError extends SalusError {}

/** A payload could not be parsed or built. */
export class PayloadError extends SalusError {}

/** The framework did not answer a request in time. */
export class SalusTimeout extends SalusError {}

/** An HTTP call to a backend route answered with a non-2xx status (see `Http.json`). */
export class HttpError extends SalusError {
  constructor(response, body) {
    super(`${response.status} ${response.statusText || ""}`.trim());
    this.status = response.status;
    this.response = response;
    /** The response body as text, if it could be read. */
    this.body = body;
  }
}

// ---------------------------------------------------------------------------
// Payload
//
// Scalars are fixed-width little-endian. Strings and byte blobs inside a compound record
// (PayloadReader / PayloadBuilder) carry a u32 LE length prefix. When a whole payload is a
// single string or JSON document it has no prefix.
// ---------------------------------------------------------------------------

const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder("utf-8", { fatal: true });

const SCALARS = {
  u8: { size: 1, min: 0, max: 0xFF, get: (v, o) => v.getUint8(o), set: (v, o, x) => v.setUint8(o, x) },
  i8: { size: 1, min: -0x80, max: 0x7F, get: (v, o) => v.getInt8(o), set: (v, o, x) => v.setInt8(o, x) },
  u16: { size: 2, min: 0, max: 0xFFFF, get: (v, o) => v.getUint16(o, true), set: (v, o, x) => v.setUint16(o, x, true) },
  i16: { size: 2, min: -0x8000, max: 0x7FFF, get: (v, o) => v.getInt16(o, true), set: (v, o, x) => v.setInt16(o, x, true) },
  u32: { size: 4, min: 0, max: 0xFFFFFFFF, get: (v, o) => v.getUint32(o, true), set: (v, o, x) => v.setUint32(o, x, true) },
  i32: { size: 4, min: -0x80000000, max: 0x7FFFFFFF, get: (v, o) => v.getInt32(o, true), set: (v, o, x) => v.setInt32(o, x, true) },
  // 64 bit integers are BigInt on the way out and accept BigInt or integer numbers on the way in.
  u64: { size: 8, big: true, min: 0n, max: (1n << 64n) - 1n, get: (v, o) => v.getBigUint64(o, true), set: (v, o, x) => v.setBigUint64(o, x, true) },
  i64: { size: 8, big: true, min: -(1n << 63n), max: (1n << 63n) - 1n, get: (v, o) => v.getBigInt64(o, true), set: (v, o, x) => v.setBigInt64(o, x, true) },
  f32: { size: 4, float: true, get: (v, o) => v.getFloat32(o, true), set: (v, o, x) => v.setFloat32(o, x, true) },
  f64: { size: 8, float: true, get: (v, o) => v.getFloat64(o, true), set: (v, o, x) => v.setFloat64(o, x, true) },
  bool: { size: 1, get: (v, o) => v.getUint8(o), set: (v, o, x) => v.setUint8(o, x) },
};

function encodeScalar(kind, value) {
  const scalar = SCALARS[kind];
  let x = value;
  if (kind === "bool") {
    x = value ? 1 : 0;
  } else if (scalar.float) {
    if (typeof x !== "number") throw new PayloadError(`cannot encode ${String(value)} as ${kind}`);
  } else if (scalar.big) {
    try {
      x = BigInt(value);
    } catch {
      throw new PayloadError(`cannot encode ${String(value)} as ${kind}`);
    }
    if (x < scalar.min || x > scalar.max) throw new PayloadError(`${value} out of range for ${kind}`);
  } else if (!Number.isInteger(x) || x < scalar.min || x > scalar.max) {
    throw new PayloadError(`cannot encode ${String(value)} as ${kind}`);
  }
  const out = new Uint8Array(scalar.size);
  scalar.set(new DataView(out.buffer), 0, x);
  return out;
}

function decodeScalar(kind, bytes, offset = 0) {
  const scalar = SCALARS[kind];
  if (offset + scalar.size > bytes.length) {
    throw new PayloadError(`need ${scalar.size} bytes for ${kind} at offset ${offset}, only ${bytes.length - offset} left`);
  }
  const value = scalar.get(new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength), offset);
  if (kind === "bool") {
    if (value > 1) throw new PayloadError(`invalid bool byte: ${value}`);
    return value === 1;
  }
  return value;
}

function decodeStr(bytes) {
  try {
    return textDecoder.decode(bytes);
  } catch {
    throw new PayloadError("not valid UTF-8");
  }
}

function encodeStr(value) {
  if (typeof value !== "string") throw new TypeError(`expected string, got ${typeof value}`);
  return textEncoder.encode(value);
}

function loads(text) {
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new PayloadError(`not valid JSON: ${e.message}`);
  }
}

function dumps(value) {
  let text;
  try {
    text = JSON.stringify(value, (_key, v) => {
      if (typeof v === "number" && !Number.isFinite(v)) throw new Error("NaN and Infinity are not valid JSON");
      if (typeof v === "bigint") throw new Error("BigInt cannot be encoded as JSON");
      return v;
    });
  } catch (e) {
    throw new PayloadError(`cannot encode as JSON: ${e.message}`);
  }
  if (text === undefined) throw new PayloadError("cannot encode as JSON: value has no JSON representation");
  return text;
}

/**
 * Anything `send()` accepts: a Payload, a string (sent as UTF-8), or raw bytes.
 * @typedef {Payload | string | Uint8Array | ArrayBuffer | ArrayBufferView | number[]} Sendable
 */

/** @param {Sendable} data @returns {Uint8Array} */
function toBytes(data) {
  if (data instanceof Payload) return data.asBytes();
  if (typeof data === "string") return textEncoder.encode(data);
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  if (Array.isArray(data)) return Uint8Array.from(data);
  throw new TypeError(`cannot send a value of type ${typeof data}; use a string, bytes or Payload.json(...)`);
}

/**
 * Immutable message payload with typed accessors.
 *
 * Parse:  asBytes(), asStr(), asJson(), asU8/I8/U16/I16/U32/I32/U64/I64/F32/F64/Bool(),
 *         reader() for compound records.
 * Build:  new Payload(bytes), Payload.str(s), Payload.json(v), Payload.u8/.../f64/bool(v),
 *         Payload.builder() for compound records.
 */
export class Payload {
  #data;

  /** @param {Uint8Array | ArrayBuffer | ArrayBufferView | number[]} [data] copied */
  constructor(data = new Uint8Array(0)) {
    this.#data = Uint8Array.from(toBytes(data));
  }

  // Wraps without copying; for bytes this module owns.
  static _wrap(bytes) {
    const payload = new Payload();
    payload.#data = bytes;
    return payload;
  }

  /** The raw bytes. Treat as read-only. */
  asBytes() {
    return this.#data;
  }

  asStr() {
    return decodeStr(this.#data);
  }

  asJson() {
    return loads(this.asStr());
  }

  reader() {
    return new PayloadReader(this.#data);
  }

  static builder() {
    return new PayloadBuilder();
  }

  static str(value) {
    return Payload._wrap(encodeStr(value));
  }

  static json(value) {
    return Payload._wrap(textEncoder.encode(dumps(value)));
  }

  get length() {
    return this.#data.length;
  }

  equals(other) {
    const bytes = other instanceof Payload ? other.#data : toBytes(other);
    return bytes.length === this.#data.length && bytes.every((b, i) => b === this.#data[i]);
  }

  toString() {
    const shown = Array.from(this.#data.subarray(0, 32), (b) => b.toString(16).padStart(2, "0")).join(" ");
    return `Payload(${this.#data.length} bytes: ${shown}${this.#data.length > 32 ? " ..." : ""})`;
  }
}

/** Reads a compound record front to back. */
export class PayloadReader {
  #data;
  #pos = 0;

  constructor(data) {
    this.#data = data;
  }

  get remaining() {
    return this.#data.length - this.#pos;
  }

  get atEnd() {
    return this.#pos >= this.#data.length;
  }

  raw(n) {
    if (n > this.remaining) {
      throw new PayloadError(`need ${n} bytes at offset ${this.#pos}, only ${this.remaining} left`);
    }
    const chunk = this.#data.subarray(this.#pos, this.#pos + n);
    this.#pos += n;
    return chunk;
  }

  rest() {
    return this.raw(this.remaining);
  }

  str() {
    return decodeStr(this.raw(this.u32()));
  }

  bytes() {
    return this.raw(this.u32());
  }

  json() {
    return loads(this.str());
  }

  expectEnd() {
    if (!this.atEnd) throw new PayloadError(`${this.remaining} unread trailing bytes`);
  }
}

/** Builds a compound record; every method returns the builder for chaining. */
export class PayloadBuilder {
  #parts = [];

  raw(data) {
    this.#parts.push(Uint8Array.from(toBytes(data)));
    return this;
  }

  str(value) {
    return this.#prefixed(encodeStr(value));
  }

  bytes(data) {
    return this.#prefixed(toBytes(data));
  }

  json(value) {
    return this.#prefixed(textEncoder.encode(dumps(value)));
  }

  build() {
    return Payload._wrap(concat(this.#parts));
  }

  #prefixed(bytes) {
    if (bytes.length > MAX_ID) throw new PayloadError(`${bytes.length} bytes do not fit a u32 length prefix`);
    this.#parts.push(encodeScalar("u32", bytes.length), Uint8Array.from(bytes));
    return this;
  }
}

function concat(chunks) {
  const out = new Uint8Array(chunks.reduce((sum, c) => sum + c.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

/**
 * Meta container used by salus:// requests: `u32 meta_len | meta (JSON object) | body`.
 * @param {object} meta
 * @param {Sendable} [body]
 */
export function packMeta(meta, body = new Uint8Array(0)) {
  const json = textEncoder.encode(dumps(meta));
  return concat([encodeScalar("u32", json.length), json, toBytes(body)]);
}

/** @returns {{meta: Record<string, any>, body: Uint8Array}} */
export function unpackMeta(data) {
  const reader = new PayloadReader(toBytes(data));
  let meta;
  try {
    meta = loads(decodeStr(reader.bytes()));
  } catch (e) {
    throw new ProtocolError(`invalid meta: ${e.message}`);
  }
  if (meta === null || typeof meta !== "object" || Array.isArray(meta)) throw new ProtocolError("meta is not a JSON object");
  return { meta, body: reader.rest() };
}

const upperFirst = (s) => s[0].toUpperCase() + s.slice(1);

// Payload.u32(v) / payload.asU32() / reader.u32() / builder.u32(v) and so on for every scalar.
for (const kind of Object.keys(SCALARS)) {
  Payload[kind] = (value) => Payload._wrap(encodeScalar(kind, value));
  Payload.prototype[`as${upperFirst(kind)}`] = function () {
    const bytes = this.asBytes();
    if (bytes.length !== SCALARS[kind].size) {
      throw new PayloadError(`payload is ${bytes.length} bytes, ${kind} needs ${SCALARS[kind].size}`);
    }
    return decodeScalar(kind, bytes);
  };
  PayloadReader.prototype[kind] = function () {
    return decodeScalar(kind, this.raw(SCALARS[kind].size));
  };
  PayloadBuilder.prototype[kind] = function (value) {
    this.raw(encodeScalar(kind, value));
    return this;
  };
}

// ---------------------------------------------------------------------------
// Wire frames, as the Salus frontend relays them: a plain object
//   { frontend_process_id, message_id, flags, logical_channel, payload: number[] | Uint8Array }
// ---------------------------------------------------------------------------

function parseFrame(data) {
  if (data === null || typeof data !== "object") return null;
  const { frontend_process_id: frontendId, message_id: messageId, flags, logical_channel: channel, payload } = data;
  if (typeof channel !== "string" || !Number.isInteger(frontendId) || !Number.isInteger(messageId)) return null;
  if (!Number.isInteger(flags)) return null;

  let bytes;
  if (payload instanceof Uint8Array) bytes = payload;
  else if (Array.isArray(payload)) bytes = Uint8Array.from(payload);
  else if (payload instanceof ArrayBuffer) bytes = new Uint8Array(payload);
  else return null;

  if (flags & ~FLAG_MORE_FRAGMENTS) throw new ProtocolError(`unknown flags: 0x${flags.toString(16)}`);
  return { frontendId, messageId, flags, channel, payload: bytes };
}

/** `peer://<id>/<name>` -> { id, name }, otherwise null. */
function parsePeerChannel(channel) {
  const match = /^peer:\/\/(\d+)\/(.+)$/.exec(channel);
  return match ? { id: Number(match[1]), name: match[2] } : null;
}

/**
 * Joins fragmented messages. Message ids are chosen by each sender independently, so the key
 * includes the sending peer (the backend counts as one sender).
 */
class Reassembler {
  #maxMessageSize;
  #partial = new Map();
  #discarding = new Set();

  constructor(maxMessageSize) {
    this.#maxMessageSize = maxMessageSize;
  }

  /** @returns {{channel: string, payload: Uint8Array, messageId: number} | null} */
  feed({ messageId, flags, channel, payload }) {
    const more = (flags & FLAG_MORE_FRAGMENTS) !== 0;
    const key = `${parsePeerChannel(channel)?.id ?? ""}/${messageId}`;

    if (this.#discarding.has(key)) {
      if (!more) this.#discarding.delete(key);
      return null;
    }

    let partial = this.#partial.get(key);
    if (partial === undefined) {
      if (!more) return { channel, payload, messageId };
      partial = { channel, chunks: [], size: 0 };
      this.#partial.set(key, partial);
    } else if (partial.channel !== channel) {
      this.#drop(key, more);
      throw new ProtocolError(`fragment of message ${messageId} switched channel from '${partial.channel}' to '${channel}'`);
    }

    partial.size += payload.length;
    if (partial.size > this.#maxMessageSize) {
      this.#drop(key, more);
      throw new ProtocolError(`message ${messageId} exceeds ${this.#maxMessageSize} bytes`);
    }
    partial.chunks.push(payload);

    if (more) return null;
    this.#partial.delete(key);
    return { channel: partial.channel, payload: concat(partial.chunks), messageId };
  }

  #drop(key, more) {
    this.#partial.delete(key);
    if (more) this.#discarding.add(key);
  }
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

/** A message received on a channel. */
export class ChannelMessage {
  constructor(channel, payload, messageId, sender = null) {
    /** Channel name without the scheme. */
    this.channel = channel;
    /** @type {Payload} */
    this.payload = payload;
    this.messageId = messageId;
    /** Frontend id of the sending peer; null for messages from the backend. */
    this.sender = sender;
    Object.freeze(this);
  }
}

function channelName(name) {
  const bare = name.startsWith(WS_PREFIX) ? name.slice(WS_PREFIX.length) : name;
  if (!bare || bare.includes("://")) throw new TypeError(`invalid channel name: '${name}'`);
  return bare;
}

/**
 * A ws:// channel. Receive with `onMessage(handler)` or by iterating:
 * `for await (const msg of channel) { ... }`. Messages are delivered while a handler is
 * attached or an iterator is active; others are dropped.
 */
export class Channel {
  #salus;
  #handlers = new Set();
  #iterators = new Set();
  #closed = false;

  constructor(salus, name, wireName, handler, peerId = null) {
    this.#salus = salus;
    /** Channel name without the scheme. */
    this.name = name;
    /** Full logical channel name on the wire, e.g. `ws://chat`. */
    this.wireName = wireName;
    /** Frontend id of the peer for `peer://` channels, otherwise null. */
    this.peerId = peerId;
    if (handler) this.#handlers.add(handler);
  }

  get closed() {
    return this.#closed;
  }

  /** @param {Sendable} data */
  send(data) {
    if (this.#closed) throw new SalusError(`channel '${this.name}' is closed`);
    this.#salus.sendMessage(this.wireName, data);
  }

  /**
   * Registers `handler(message)`; async handlers are fine, errors are logged and do not
   * affect other messages.
   * @param {(message: ChannelMessage) => void | Promise<void>} handler
   * @returns {() => void} unsubscribe
   */
  onMessage(handler) {
    this.#handlers.add(handler);
    return () => this.#handlers.delete(handler);
  }

  /** Unregisters the channel and ends all iterators. */
  close() {
    if (this.#closed) return;
    this.#closed = true;
    this.#handlers.clear();
    this.#salus._removeChannel(this);
    for (const iterator of this.#iterators) iterator.end();
    this.#iterators.clear();
  }

  [Symbol.asyncIterator]() {
    const queue = new MessageQueue();
    if (this.#closed) queue.end();
    this.#iterators.add(queue);
    return {
      next: () => queue.next(),
      return: () => {
        this.#iterators.delete(queue);
        queue.end();
        return Promise.resolve({ value: undefined, done: true });
      },
    };
  }

  _deliver(message) {
    for (const handler of [...this.#handlers]) {
      try {
        Promise.resolve(handler(message)).catch((e) => console.error(`Salus: handler for channel '${this.name}' failed`, e));
      } catch (e) {
        console.error(`Salus: handler for channel '${this.name}' failed`, e);
      }
    }
    for (const iterator of this.#iterators) iterator.push(message);
  }

  _hasReceivers() {
    return this.#handlers.size > 0 || this.#iterators.size > 0;
  }
}

class MessageQueue {
  #items = [];
  #waiting = null;
  #ended = false;

  push(item) {
    if (this.#ended) return;
    if (this.#waiting) {
      const resolve = this.#waiting;
      this.#waiting = null;
      resolve({ value: item, done: false });
    } else {
      this.#items.push(item);
    }
  }

  end() {
    this.#ended = true;
    if (this.#waiting) {
      const resolve = this.#waiting;
      this.#waiting = null;
      resolve({ value: undefined, done: true });
    }
  }

  next() {
    if (this.#items.length > 0) return Promise.resolve({ value: this.#items.shift(), done: false });
    if (this.#ended) return Promise.resolve({ value: undefined, done: true });
    return new Promise((resolve) => {
      this.#waiting = resolve;
    });
  }
}

// ---------------------------------------------------------------------------
// Peers: other plugin frontends this one may talk to. A frontend that opened a dependency
// (`openDependency`) and that dependency (`salus.parent`) are peers; the framework refuses
// messages between any other frontends.
// ---------------------------------------------------------------------------

export class Peer {
  #salus;

  constructor(salus, id, component = null) {
    this.#salus = salus;
    /** Frontend process id of the peer. */
    this.id = id;
    /** Component name of the peer if known (components of your own plugin; opened components). */
    this.component = component;
  }

  /**
   * Registers the peer channel `name`: messages the peer sends with `peer.send(name, ...)` arrive here
   * (`message.sender` is the peer id). Same interface as `salus.channel()`.
   * @param {string} name
   * @param {(message: ChannelMessage) => void | Promise<void>} [handler]
   */
  channel(name, handler) {
    return this.#salus._peerChannel(this.id, name, handler);
  }

  /**
   * Sends to the channel `name` of the peer. Messages for channels the peer has not registered are dropped.
   * @param {string} name
   * @param {Sendable} data
   */
  send(name, data) {
    this.#salus.sendMessage(`${PEER_PREFIX}${this.id}/${peerChannelName(name)}`, data);
  }
}

function peerChannelName(name) {
  if (typeof name !== "string" || name === "") throw new TypeError(`invalid channel name: '${String(name)}'`);
  return name;
}

// ---------------------------------------------------------------------------
// HTTP
//
// The backend opens routes (`@plugin.route("GET", "/doc/{id}")`); the framework serves them
// to this frontend under /plugin-api/<frontend id>. Plain `fetch` works too; this saves
// building the prefix and covers the common cases.
// ---------------------------------------------------------------------------

/**
 * @typedef {object} HttpOptions
 * @property {Record<string, string | number | boolean | null | undefined | Array<string | number | boolean>>} [query]
 * @property {Record<string, string>} [headers]
 * @property {BodyInit | Payload | null} [body] raw request body
 * @property {unknown} [json] request body encoded as JSON (sets content-type)
 * @property {AbortSignal} [signal]
 */

export class Http {
  #base;
  #fetch;

  constructor(frontendId, { base = "", fetch: fetchFn } = {}) {
    this.#base = `${base}/plugin-api/${frontendId}`;
    this.#fetch = fetchFn;
  }

  /**
   * URL of a backend route; use it directly for `<img src>`, `<a href>`, downloads and so on
   * (GET requests carry the session cookie automatically).
   * Encode dynamic path segments yourself with `encodeURIComponent`.
   * @param {string} path starts with "/"
   * @param {HttpOptions["query"]} [query]
   */
  url(path, query) {
    if (typeof path !== "string" || !path.startsWith("/")) throw new TypeError(`route path must start with '/': ${String(path)}`);
    // The framework serves the plugin root without a trailing slash.
    let url = path === "/" ? this.#base : this.#base + path;
    const search = encodeQuery(query);
    if (search) url += (url.includes("?") ? "&" : "?") + search;
    return url;
  }

  /**
   * Sends a request and returns the standard `Response`. Like `fetch`, error statuses do not throw.
   * @param {string} method
   * @param {string} path
   * @param {HttpOptions} [options]
   * @returns {Promise<Response>}
   */
  request(method, path, { query, headers, body, json, signal } = {}) {
    const init = { method: method.toUpperCase(), headers: { ...headers }, credentials: "same-origin", signal };
    if (json !== undefined) {
      if (body !== undefined && body !== null) throw new TypeError("pass either `body` or `json`, not both");
      init.body = dumps(json);
      if (!hasHeader(init.headers, "content-type")) init.headers["content-type"] = "application/json";
    } else if (body !== undefined && body !== null) {
      init.body = body instanceof Payload ? body.asBytes() : body;
    }
    return this.#fetch(this.url(path, query), init);
  }

  get(path, options) {
    return this.request("GET", path, options);
  }

  post(path, options) {
    return this.request("POST", path, options);
  }

  put(path, options) {
    return this.request("PUT", path, options);
  }

  patch(path, options) {
    return this.request("PATCH", path, options);
  }

  delete(path, options) {
    return this.request("DELETE", path, options);
  }

  /**
   * Sends a request and returns the parsed JSON body (`undefined` for 204 or an empty body).
   * Throws `HttpError` for non-2xx responses.
   * @param {string} method
   * @param {string} path
   * @param {HttpOptions} [options]
   */
  async json(method, path, options = {}) {
    const response = await this.request(method, path, {
      ...options,
      headers: { accept: "application/json", ...options.headers },
    });
    if (!response.ok) {
      let body;
      try {
        body = await response.text();
      } catch {
        body = undefined;
      }
      throw new HttpError(response, body);
    }
    const text = await response.text();
    return text === "" ? undefined : loads(text);
  }
}

function hasHeader(headers, name) {
  return Object.keys(headers).some((key) => key.toLowerCase() === name);
}

function encodeQuery(query) {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== null) params.append(key, String(item));
    }
  }
  return params.toString();
}

// ---------------------------------------------------------------------------
// Salus
// ---------------------------------------------------------------------------

function frontendIdFromUrl(win) {
  const raw = new URLSearchParams(win.location.search).get("fe_process_id");
  const id = raw === null ? NaN : Number(raw);
  return Number.isInteger(id) ? id : undefined;
}

/** The `params` the component was opened with (JSON in the URL), or null. */
function paramsFromUrl(win) {
  const raw = new URLSearchParams(win.location.search).get("params");
  if (raw === null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function parentIdFromUrl(win) {
  const raw = new URLSearchParams(win.location.search).get("parent_fe_process_id");
  const id = raw === null ? NaN : Number(raw);
  return Number.isInteger(id) ? id : null;
}

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
export class Salus {
  #window;
  #parentOrigin;
  #maxFragmentPayload;
  #reassembler;
  #channels = new Map(); // keyed by wire name
  #unclaimed = new Set();
  #parentId;
  #peers = new Map();
  #readyPeers = new Set();
  #readyWaiters = new Map();
  #pending = new Map();
  #nextRequestId = 0;
  #nextMessageId = 0;
  #listener;
  #closed = false;

  /** @param {SalusOptions} [options] */
  constructor(options = {}) {
    const win = (this.#window = options.window ?? globalThis.window);
    if (!win || !win.parent || win.parent === win) {
      throw new SalusError("Salus SDK must run inside a plugin iframe of the Salus frontend");
    }

    const frontendId = options.frontendId ?? frontendIdFromUrl(win);
    if (!Number.isInteger(frontendId) || frontendId < 1 || frontendId >= MAX_ID) {
      throw new SalusError("could not determine the frontend process id; the page URL needs ?fe_process_id=<id> (Salus adds it), or pass { frontendId }");
    }
    /** Id of this frontend process; identifies it towards the backend. */
    this.frontendId = frontendId;

    this.#parentOrigin = options.parentOrigin ?? win.location.origin;
    this.#maxFragmentPayload = options.maxFragmentPayload ?? DEFAULT_MAX_FRAGMENT_PAYLOAD;
    this.#reassembler = new Reassembler(options.maxMessageSize ?? DEFAULT_MAX_MESSAGE_SIZE);

    /** HTTP access to the routes the backend opened. */
    this.http = new Http(frontendId, { fetch: options.fetch ?? ((...args) => win.fetch(...args)) });

    this.#parentId = options.parentFrontendId ?? parentIdFromUrl(win);
    /** Name of this frontend's component (from the manifest), or null if unknown. */
    this.component = new URLSearchParams(win.location.search).get("component");
    /** What this component was opened with (`params` of `openComponent`, or `{file}` for viewers), or null. */
    this.params = paramsFromUrl(win);

    this.#listener = (event) => this.#onWindowMessage(event);
    win.addEventListener("message", this.#listener);

    if (this.#parentId !== null) {
      // After the plugin's own startup code ran and registered its channels.
      setTimeout(() => {
        try {
          this.sendMessage(`${PEER_PREFIX}${this.#parentId}/${READY_CHANNEL}`, "");
        } catch {
          // closed in the meantime
        }
      }, 0);
    }
  }

  /** @param {SalusOptions} [options] */
  static connect(options) {
    return new Salus(options);
  }

  /**
   * Registers the ws://<name> channel. Throws if it is already registered.
   * @param {string} name
   * @param {(message: ChannelMessage) => void | Promise<void>} [handler]
   */
  channel(name, handler) {
    const bare = channelName(name);
    const wireName = WS_PREFIX + bare;
    if (this.#channels.has(wireName)) throw new SalusError(`channel '${bare}' is already registered`);
    const channel = new Channel(this, bare, wireName, handler);
    this.#channels.set(wireName, channel);
    return channel;
  }

  _peerChannel(peerId, name, handler) {
    const bare = peerChannelName(name);
    const wireName = `${PEER_PREFIX}${peerId}/${bare}`;
    if (this.#channels.has(wireName)) throw new SalusError(`channel '${bare}' of peer ${peerId} is already registered`);
    const channel = new Channel(this, bare, wireName, handler, peerId);
    this.#channels.set(wireName, channel);
    return channel;
  }

  /** The frontend that opened this one as a dependency, or null if this frontend was opened by the user. */
  get parent() {
    return this.#parentId === null ? null : this.peer(this.#parentId);
  }

  /**
   * Handle for the frontend with the given id; it must be this frontend's parent or one of its
   * dependencies for messages to get through.
   * @param {number} frontendId
   */
  peer(frontendId, component = null) {
    if (!Number.isInteger(frontendId)) throw new TypeError("frontend id must be an integer");
    let peer = this.#peers.get(frontendId);
    if (peer === undefined) {
      peer = new Peer(this, frontendId, component);
      this.#peers.set(frontendId, peer);
    } else if (component !== null) {
      peer.component = component;
    }
    return peer;
  }

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
  async openDependency(pluginIdentifier, { component, params, reuse = false, timeout = DEFAULT_REQUEST_TIMEOUT } = {}) {
    return this.#openFrontend("dependency/open", { plugin: pluginIdentifier, component, params, reuse }, timeout, pluginIdentifier);
  }

  /**
   * Opens another frontend component of this plugin (names from the manifest's `[[frontend-component]]`)
   * in its `target-panel` and resolves with a Peer for it once it has loaded. No dependency declaration needed.
   * @param {string} componentName
   * @param {{params?: any, reuse?: boolean, timeout?: number}} [options]
   * @returns {Promise<Peer>}
   */
  async openComponent(componentName, { params, reuse = false, timeout = DEFAULT_REQUEST_TIMEOUT } = {}) {
    return this.#openFrontend("component/open", { component: componentName, params, reuse }, timeout, componentName);
  }

  async #openFrontend(topic, request, timeout, label) {
    const { meta } = await this.request(topic, request, { timeout });
    const frontendId = meta.frontend_id;
    if (!Number.isInteger(frontendId)) throw new SalusError("the framework did not return a frontend id");
    // A component that was already running has announced itself long ago.
    if (meta.reused !== true) await this.#waitUntilReady(frontendId, timeout, label);
    return this.peer(frontendId, typeof meta.component === "string" ? meta.component : null);
  }

  /**
   * The other components of this plugin that are currently open (optionally only those named `name`).
   * You can message them like any peer.
   * @param {string} [name]
   * @returns {Promise<Peer[]>}
   */
  async components(name) {
    const { meta } = await this.request("component/list", name === undefined ? {} : { component: name });
    return (meta.components ?? []).map((c) => this.peer(c.frontend_id, c.component));
  }

  /**
   * Asks Salus to open a file in a viewer. Salus finds the viewers for the file type and lets the user
   * choose if there are several; you never see which. Rejects with `SalusError` if no viewer exists or the
   * user cancelled. The path is passed on as is, so use what the viewer's backend can open.
   * @param {string} path
   * @returns {Promise<void>}
   */
  async openFile(path) {
    if (typeof path !== "string" || path === "") throw new TypeError("path must be a non-empty string");
    // Generous timeout: the user may be choosing a viewer.
    await this.request("file/open", { file: path }, { timeout: 120_000 });
  }

  #waitUntilReady(frontendId, timeout, pluginIdentifier) {
    if (this.#readyPeers.has(frontendId)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const waiters = this.#readyWaiters.get(frontendId) ?? [];
      const waiter = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        this.#readyWaiters.set(frontendId, (this.#readyWaiters.get(frontendId) ?? []).filter((w) => w !== waiter));
        reject(new SalusTimeout(`plugin '${pluginIdentifier}' did not finish loading within ${timeout} ms`));
      }, timeout);
      waiters.push(waiter);
      this.#readyWaiters.set(frontendId, waiters);
    });
  }

  #markReady(frontendId) {
    this.#readyPeers.add(frontendId);
    for (const waiter of this.#readyWaiters.get(frontendId) ?? []) waiter();
    this.#readyWaiters.delete(frontendId);
  }

  /**
   * Sends a request to the framework on salus://<topic> and resolves with its reply
   * `{meta, body}`. Rejects with SalusError if the framework refuses and SalusTimeout if it does not answer.
   * @param {string} topic
   * @param {Record<string, any>} [params]
   * @param {{timeout?: number}} [options]
   */
  request(topic, params = {}, { timeout = DEFAULT_REQUEST_TIMEOUT } = {}) {
    if ("id" in params) throw new TypeError("'id' is reserved for request correlation");
    if (this.#closed) return Promise.reject(new SalusError("connection is closed"));

    const id = ++this.#nextRequestId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new SalusTimeout(`no reply to salus://${topic} within ${timeout} ms`));
      }, timeout);
      this.#pending.set(id, { resolve, reject, timer });
      try {
        this.sendMessage(SALUS_PREFIX + topic, packMeta({ id, ...params }));
      } catch (e) {
        clearTimeout(timer);
        this.#pending.delete(id);
        reject(e);
      }
    });
  }

  #handleControlReply(message) {
    let parsed;
    try {
      parsed = unpackMeta(message.payload);
    } catch (e) {
      console.warn(`Salus: dropping malformed ${message.channel} message: ${e.message}`);
      return true;
    }
    const { meta, body } = parsed;
    if (!Number.isInteger(meta.id) || !("ok" in meta)) return false;

    const pending = this.#pending.get(meta.id);
    if (pending === undefined) {
      console.warn(`Salus: unexpected reply ${meta.id} on ${message.channel} (request already timed out?)`);
      return true;
    }
    this.#pending.delete(meta.id);
    clearTimeout(pending.timer);
    if (meta.ok === true) pending.resolve({ meta, body: Payload._wrap(body) });
    else pending.reject(new SalusError(String(meta.error ?? "request failed")));
    return true;
  }

  /**
   * Sends on ws://<name> without registering the channel.
   * @param {string} name
   * @param {Sendable} data
   */
  send(name, data) {
    this.sendMessage(WS_PREFIX + channelName(name), data);
  }

  /**
   * Low level: sends on any logical channel name, fragmenting if the message is large.
   * @param {string} logicalChannel full name including the scheme
   * @param {Sendable} data
   */
  sendMessage(logicalChannel, data) {
    if (this.#closed) throw new SalusError("connection is closed");
    const channelLen = textEncoder.encode(logicalChannel).length;
    if (channelLen > MAX_CHANNEL_LEN) throw new ProtocolError(`channel name too long: ${channelLen} bytes`);

    const bytes = toBytes(data);
    const room = Math.max(1, Math.min(this.#maxFragmentPayload, MAX_FRAME_SIZE - FRAME_HEADER_LEN - channelLen));
    const messageId = this.#nextMessageId;
    this.#nextMessageId = (messageId + 1) % (MAX_ID + 1);

    // All fragments are posted back to back, so they reach the backend in order and
    // the message id can be reused by the next message.
    let offset = 0;
    do {
      const chunk = bytes.subarray(offset, offset + room);
      offset += chunk.length;
      this.#window.parent.postMessage(
        {
          frontend_process_id: this.frontendId,
          message_id: messageId,
          flags: offset < bytes.length ? FLAG_MORE_FRAGMENTS : 0,
          logical_channel: logicalChannel,
          payload: Array.from(chunk),
        },
        this.#parentOrigin,
      );
    } while (offset < bytes.length);
  }

  /**
   * Handler for complete messages on channels that are not registered (including other
   * schemes). Without one, such messages are logged and dropped.
   * @param {(message: {channel: string, payload: Payload, messageId: number}) => void} handler
   * @returns {() => void} unsubscribe
   */
  onUnclaimed(handler) {
    this.#unclaimed.add(handler);
    return () => this.#unclaimed.delete(handler);
  }

  /** Stops listening and closes all channels. */
  close() {
    if (this.#closed) return;
    this.#closed = true;
    this.#window.removeEventListener("message", this.#listener);
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new SalusError("connection is closed"));
    }
    this.#pending.clear();
    for (const channel of [...this.#channels.values()]) channel.close();
  }

  _removeChannel(channel) {
    if (this.#channels.get(channel.wireName) === channel) this.#channels.delete(channel.wireName);
  }

  #onWindowMessage(event) {
    // Only the Salus frontend that embeds us may talk to us.
    if (event.source !== this.#window.parent || event.origin !== this.#parentOrigin) return;

    let message;
    try {
      const frame = parseFrame(event.data);
      if (frame === null) return;
      if (frame.frontendId !== this.frontendId) {
        console.warn(`Salus: dropping frame addressed to frontend ${frame.frontendId}`);
        return;
      }
      message = this.#reassembler.feed(frame);
    } catch (e) {
      console.warn("Salus: dropping invalid frame:", e.message);
      return;
    }
    if (message !== null) this.#route(message);
  }

  #route(message) {
    if (message.channel.startsWith(SALUS_PREFIX) && this.#handleControlReply(message)) return;

    const peer = parsePeerChannel(message.channel);
    if (peer !== null && peer.name === READY_CHANNEL) {
      this.#markReady(peer.id);
      return;
    }

    const channel = this.#channels.get(message.channel);
    if (channel !== undefined) {
      if (channel._hasReceivers()) {
        channel._deliver(new ChannelMessage(channel.name, Payload._wrap(message.payload), message.messageId, channel.peerId));
      }
      return;
    }

    if (this.#unclaimed.size === 0) {
      console.warn(`Salus: no handler for channel '${message.channel}'; dropping message`);
      return;
    }
    const unclaimed = { channel: message.channel, payload: Payload._wrap(message.payload), messageId: message.messageId };
    for (const handler of [...this.#unclaimed]) {
      try {
        handler(unclaimed);
      } catch (e) {
        console.error("Salus: unclaimed-message handler failed", e);
      }
    }
  }
}
