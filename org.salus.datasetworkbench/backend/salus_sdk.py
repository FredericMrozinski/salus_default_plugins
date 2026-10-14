from __future__ import annotations

import asyncio
import contextvars
import inspect
import itertools
import json
import logging
import os
import struct
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from urllib.parse import parse_qs

logger = logging.getLogger("SALUS SDK")


def _get_log_file_path():
    now = datetime.now()
    tmpfolder = tempfile.gettempdir()
    return os.path.join(tmpfolder, "salus_sdk_log_" + now.strftime("%Y_%m_%d__%H_%M_%S") + ".log")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s - %(levelname)s]\t%(message)s',
        handlers=[
            logging.FileHandler(_get_log_file_path()),
            logging.StreamHandler(),
        ],
    )


# ---------------------------------------------------------------------------
# Wire format
#
#   [u32 LE frame_len]                 outer length prefix, counts the bytes below
#   [u32 LE frontend_process_id]
#   [u32 LE message_id]                unique per sender among in-flight messages
#   [u8     flags]                     bit0 = MORE_FRAGMENTS
#   [u16 LE channel_len][channel]      UTF-8
#   [payload]                          rest of the frame
# ---------------------------------------------------------------------------

LENGTH_PREFIX = struct.Struct("<I")
_HEADER = struct.Struct("<IIBH")

MAX_FRAME_SIZE = 64 * 1024 * 1024
MAX_CHANNEL_LEN = 0xFFFF
MAX_ID = 0xFFFFFFFF

# Reserved frontend id: deliver to every frontend bound to this backend.
ALL = MAX_ID

# Frontend id used on frames addressed to the framework itself.
NO_FRONTEND = 0

WS_PREFIX = "ws://"
SALUS_PREFIX = "salus://"
HTTP_PREFIX = "http://"
HTTP_RESPONSE_CHANNEL = "http://response"

FLAG_MORE_FRAGMENTS = 0x01
_KNOWN_FLAGS = FLAG_MORE_FRAGMENTS


class ProtocolError(Exception):
    """A frame violates the wire format."""


@dataclass(frozen=True)
class Frame:
    frontend_id: int
    message_id: int
    channel: str
    payload: bytes = b""
    flags: int = 0

    @property
    def more_fragments(self) -> bool:
        return bool(self.flags & FLAG_MORE_FRAGMENTS)


def encode_frame(frame: Frame) -> bytes:
    """Encode a frame body, without the outer length prefix."""
    if not 0 <= frame.frontend_id <= MAX_ID:
        raise ProtocolError(f"frontend_id out of range: {frame.frontend_id}")
    if not 0 <= frame.message_id <= MAX_ID:
        raise ProtocolError(f"message_id out of range: {frame.message_id}")
    if frame.flags & ~_KNOWN_FLAGS:
        raise ProtocolError(f"unknown flags: {frame.flags:#04x}")

    channel = frame.channel.encode("utf-8")
    if len(channel) > MAX_CHANNEL_LEN:
        raise ProtocolError(f"channel name too long: {len(channel)} bytes")

    body = b"".join((
        _HEADER.pack(frame.frontend_id, frame.message_id, frame.flags, len(channel)),
        channel,
        frame.payload,
    ))
    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame too large: {len(body)} bytes (max {MAX_FRAME_SIZE})")
    return body


def decode_frame(body: bytes) -> Frame:
    """Decode a frame body, without the outer length prefix."""
    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame too large: {len(body)} bytes (max {MAX_FRAME_SIZE})")
    if len(body) < _HEADER.size:
        raise ProtocolError(f"frame shorter than header: {len(body)} bytes")

    frontend_id, message_id, flags, channel_len = _HEADER.unpack_from(body)
    if flags & ~_KNOWN_FLAGS:
        raise ProtocolError(f"unknown flags: {flags:#04x}")

    channel_end = _HEADER.size + channel_len
    if channel_end > len(body):
        raise ProtocolError("channel name runs past end of frame")
    try:
        channel = body[_HEADER.size:channel_end].decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProtocolError(f"channel name is not valid UTF-8: {e}") from None

    return Frame(frontend_id, message_id, channel, bytes(body[channel_end:]), flags)


def pack_frame(frame: Frame) -> bytes:
    """Encode a frame including the outer length prefix, ready to write to the socket."""
    body = encode_frame(frame)
    return LENGTH_PREFIX.pack(len(body)) + body


def check_frame_length(length: int) -> int:
    """Validate a length prefix read from the socket before allocating for it."""
    if length > MAX_FRAME_SIZE:
        raise ProtocolError(f"peer announced a {length} byte frame (max {MAX_FRAME_SIZE})")
    return length


# ---------------------------------------------------------------------------
# Payload
#
# Scalars are fixed-width little-endian. Strings and byte blobs inside a compound
# record (PayloadReader / PayloadBuilder) carry a u32 LE length prefix. When a whole
# payload is a single string or JSON document it has no prefix.
# ---------------------------------------------------------------------------

class PayloadError(ValueError):
    """A payload could not be parsed or built."""


_SCALARS = {
    "u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i",
    "u64": "<Q", "i64": "<q", "f32": "<f", "f64": "<d", "bool": "<?",
}
_SCALAR_STRUCTS = {kind: struct.Struct(fmt) for kind, fmt in _SCALARS.items()}
_U32 = _SCALAR_STRUCTS["u32"]


def _pack_scalar(kind: str, value) -> bytes:
    try:
        return _SCALAR_STRUCTS[kind].pack(bool(value) if kind == "bool" else value)
    except (struct.error, OverflowError) as e:
        raise PayloadError(f"cannot encode {value!r} as {kind}: {e}") from None


def _unpack_scalar(kind: str, data: bytes, offset: int = 0):
    if kind == "bool" and data[offset] > 1:
        raise PayloadError(f"invalid bool byte: {data[offset]}")
    return _SCALAR_STRUCTS[kind].unpack_from(data, offset)[0]


def _decode_str(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PayloadError(f"not valid UTF-8: {e}") from None


def _encode_str(value) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    return value.encode("utf-8")


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _loads(text: str):
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as e:
        raise PayloadError(f"not valid JSON: {e}") from None


def _dumps(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise PayloadError(f"cannot encode as JSON: {e}") from None


class Payload:
    """Immutable message payload with typed accessors.

    Parse:  as_bytes(), as_str(), as_json(), as_u8/i8/u16/i16/u32/i32/u64/i64/f32/f64/bool(),
            reader() for compound records.
    Build:  Payload(b"..."), Payload.str(s), Payload.json(obj), Payload.u8/.../f64/bool(v),
            Payload.builder() for compound records.
    """

    __slots__ = ("_data",)

    def __init__(self, data: bytes | bytearray | memoryview = b""):
        self._data = data if type(data) is bytes else bytes(data)

    def as_bytes(self) -> bytes:
        return self._data

    def as_str(self) -> str:
        return _decode_str(self._data)

    def as_json(self):
        return _loads(self.as_str())

    def reader(self) -> PayloadReader:
        return PayloadReader(self._data)

    @staticmethod
    def builder() -> PayloadBuilder:
        return PayloadBuilder()

    @classmethod
    def str(cls, value: str) -> Payload:
        return cls(_encode_str(value))

    @classmethod
    def json(cls, value) -> Payload:
        return cls(_dumps(value).encode("utf-8"))

    def __len__(self) -> int:
        return len(self._data)

    def __bytes__(self) -> bytes:
        return self._data

    def __eq__(self, other) -> bool:
        if isinstance(other, Payload):
            return self._data == other._data
        if isinstance(other, (bytes, bytearray, memoryview)):
            return self._data == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._data)

    def __repr__(self) -> str:
        shown = self._data[:32]
        return f"Payload({len(self._data)} bytes: {shown!r}{'...' if len(self._data) > 32 else ''})"


class PayloadReader:
    """Reads a compound record front to back."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    @property
    def at_end(self) -> bool:
        return self._pos >= len(self._data)

    def raw(self, n: int) -> bytes:
        if n > self.remaining:
            raise PayloadError(f"need {n} bytes at offset {self._pos}, only {self.remaining} left")
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk

    def rest(self) -> bytes:
        return self.raw(self.remaining)

    def str(self) -> str:
        return _decode_str(self.raw(self.u32()))

    def bytes(self) -> bytes:
        return self.raw(self.u32())

    def json(self):
        return _loads(self.str())

    def expect_end(self) -> None:
        if not self.at_end:
            raise PayloadError(f"{self.remaining} unread trailing bytes")


class PayloadBuilder:
    """Builds a compound record; every method returns the builder for chaining."""

    def __init__(self):
        self._parts: list[bytes] = []

    def raw(self, data: bytes | bytearray | memoryview) -> PayloadBuilder:
        self._parts.append(bytes(data))
        return self

    def str(self, value: str) -> PayloadBuilder:
        return self._prefixed(_encode_str(value))

    def bytes(self, data: bytes | bytearray | memoryview) -> PayloadBuilder:
        return self._prefixed(bytes(data))

    def json(self, value) -> PayloadBuilder:
        return self._prefixed(_dumps(value).encode("utf-8"))

    def build(self) -> Payload:
        return Payload(b"".join(self._parts))

    def _prefixed(self, data: bytes) -> PayloadBuilder:
        if len(data) > MAX_ID:
            raise PayloadError(f"{len(data)} bytes do not fit a u32 length prefix")
        self._parts.append(_U32.pack(len(data)))
        self._parts.append(data)
        return self


def _install_scalar_methods() -> None:
    for kind, st in _SCALAR_STRUCTS.items():
        def as_kind(self, _kind=kind, _st=st):
            if len(self._data) != _st.size:
                raise PayloadError(f"payload is {len(self._data)} bytes, {_kind} needs {_st.size}")
            return _unpack_scalar(_kind, self._data)

        def make(cls, value, _kind=kind):
            return cls(_pack_scalar(_kind, value))

        def read(self, _kind=kind, _st=st):
            return _unpack_scalar(_kind, self.raw(_st.size))

        def write(self, value, _kind=kind):
            self._parts.append(_pack_scalar(_kind, value))
            return self

        as_kind.__name__ = f"as_{kind}"
        for name, fn, owner in (
            (f"as_{kind}", as_kind, Payload),
            (kind, classmethod(make), Payload),
            (kind, read, PayloadReader),
            (kind, write, PayloadBuilder),
        ):
            setattr(owner, name, fn)


_install_scalar_methods()


# ---------------------------------------------------------------------------
# Meta payloads, used by http:// and salus:// channels:
#   [u32 LE meta_len][meta: UTF-8 JSON object][body: rest of the payload]
# ---------------------------------------------------------------------------

def pack_meta(meta: dict, body: bytes | bytearray | memoryview = b"") -> bytes:
    meta_bytes = _dumps(meta).encode("utf-8")
    return b"".join((_U32.pack(len(meta_bytes)), meta_bytes, body))


def unpack_meta(data: bytes) -> tuple[dict, bytes]:
    if len(data) < _U32.size:
        raise ProtocolError("meta payload shorter than its length prefix")
    (meta_len,) = _U32.unpack_from(data)
    end = _U32.size + meta_len
    if end > len(data):
        raise ProtocolError("meta runs past end of payload")
    try:
        meta = _loads(_decode_str(data[_U32.size:end]))
    except PayloadError as e:
        raise ProtocolError(f"invalid meta: {e}") from None
    if not isinstance(meta, dict):
        raise ProtocolError("meta is not a JSON object")
    return meta, data[end:]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

MAX_MESSAGE_SIZE = 1024 * 1024 * 1024

# A single multi-MiB StreamWriter.write() is extremely slow on macOS (~5 MiB/s),
# slicing it keeps throughput at several hundred MiB/s.
_WRITE_SLICE = 1024 * 1024


class ConnectionClosed(ConnectionError):
    """The connection to the framework is closed."""


@dataclass(frozen=True)
class Message:
    """A complete message, with all fragments already reassembled."""
    frontend_id: int
    channel: str
    payload: Payload
    message_id: int = 0


class _Partial:
    __slots__ = ("channel", "chunks", "size")

    def __init__(self, channel: str):
        self.channel = channel
        self.chunks: list[bytes] = []
        self.size = 0


class _Reassembler:
    """Joins fragmented messages. Messages are keyed by (frontend_id, message_id),
    so fragments of different messages may be interleaved arbitrarily."""

    def __init__(self, max_message_size: int):
        self._max_message_size = max_message_size
        self._partial: dict[tuple[int, int], _Partial] = {}
        self._discarding: set[tuple[int, int]] = set()

    def feed(self, frame: Frame) -> Message | None:
        key = (frame.frontend_id, frame.message_id)

        if key in self._discarding:
            if not frame.more_fragments:
                self._discarding.discard(key)
            return None

        partial = self._partial.get(key)
        if partial is None:
            if not frame.more_fragments:
                return Message(frame.frontend_id, frame.channel, Payload(frame.payload), frame.message_id)
            partial = self._partial[key] = _Partial(frame.channel)
        elif partial.channel != frame.channel:
            self._drop(key, frame)
            raise ProtocolError(
                f"fragment of message {frame.message_id} switched channel "
                f"from {partial.channel!r} to {frame.channel!r}")

        partial.size += len(frame.payload)
        if partial.size > self._max_message_size:
            self._drop(key, frame)
            raise ProtocolError(
                f"message {frame.message_id} exceeds {self._max_message_size} bytes")
        partial.chunks.append(frame.payload)

        if frame.more_fragments:
            return None
        del self._partial[key]
        return Message(frame.frontend_id, partial.channel, Payload(b"".join(partial.chunks)), frame.message_id)

    def _drop(self, key: tuple[int, int], frame: Frame) -> None:
        self._partial.pop(key, None)
        if frame.more_fragments:
            self._discarding.add(key)


Sendable = Payload | str | bytes | bytearray | memoryview


def _to_buffer(data: Sendable):
    if isinstance(data, Payload):
        return data.as_bytes()
    if isinstance(data, str):
        return data.encode("utf-8")
    return data


_EOF = object()


class Connection:
    """One framed, multiplexed connection to the framework.

    Must be created and used on the asyncio event loop that owns it."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_fragment_payload: int | None = None,
        max_message_size: int = MAX_MESSAGE_SIZE,
    ):
        self._reader = reader
        self._writer = writer
        self._max_fragment_payload = max_fragment_payload
        self._reassembler = _Reassembler(max_message_size)
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._next_message_id = 0
        self._outgoing_ids: set[int] = set()
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._read_task = asyncio.create_task(self._read_loop(), name="salus-reader")

    @classmethod
    async def open(cls, socket_path: str, **kwargs) -> "Connection":
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=1024 * 1024)
        return cls(reader, writer, **kwargs)

    async def messages(self) -> AsyncIterator[Message]:
        """Complete messages in the order they finished arriving. Ends when the connection closes."""
        while True:
            item = await self._inbox.get()
            if item is _EOF:
                self._inbox.put_nowait(_EOF)
                return
            yield item

    async def send_message(self, frontend_id: int, channel: str, data: Sendable) -> None:
        """Send one message, split into fragments if it does not fit into a single frame."""
        if self._closed:
            raise ConnectionClosed("connection is closed")

        channel_len = len(channel.encode("utf-8"))
        room = self._max_fragment_payload or (MAX_FRAME_SIZE - _HEADER.size - channel_len)
        view = memoryview(_to_buffer(data)).cast("B")
        message_id = self._allocate_message_id()
        try:
            offset = 0
            while True:
                chunk = view[offset:offset + room]
                offset += len(chunk)
                last = offset >= len(view)
                body = encode_frame(Frame(
                    frontend_id, message_id, channel, chunk, 0 if last else FLAG_MORE_FRAGMENTS))
                await asyncio.shield(self._write_frame(body))
                if last:
                    return
        finally:
            self._outgoing_ids.discard(message_id)

    async def _write_frame(self, body: bytes) -> None:
        # Frames are written one at a time, so slices of different frames never mix.
        # Callers shield this: cancelling mid-frame would leave a torn frame on the socket.
        view = memoryview(body)
        async with self._write_lock:
            self._writer.write(LENGTH_PREFIX.pack(len(view)))
            for offset in range(0, len(view), _WRITE_SLICE):
                self._writer.write(view[offset:offset + _WRITE_SLICE])
                await self._writer.drain()

    async def wait_closed(self) -> None:
        """Returns when the peer closes the connection cleanly; raises if it failed."""
        await asyncio.shield(self._read_task)

    async def close(self) -> None:
        self._closed = True
        self._read_task.cancel()
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except OSError:
            pass
        await asyncio.gather(self._read_task, return_exceptions=True)

    def _allocate_message_id(self) -> int:
        while True:
            message_id = self._next_message_id
            self._next_message_id = (message_id + 1) & MAX_ID
            if message_id not in self._outgoing_ids:
                self._outgoing_ids.add(message_id)
                return message_id

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    prefix = await self._reader.readexactly(LENGTH_PREFIX.size)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        raise ConnectionClosed("connection closed inside a length prefix") from None
                    return

                (length,) = LENGTH_PREFIX.unpack(prefix)
                check_frame_length(length)
                try:
                    body = await self._reader.readexactly(length)
                except asyncio.IncompleteReadError:
                    raise ConnectionClosed("connection closed inside a frame") from None

                try:
                    message = self._reassembler.feed(decode_frame(body))
                except ProtocolError as e:
                    logger.warning("Dropping invalid frame: %s", e)
                    continue
                if message is not None:
                    self._inbox.put_nowait(message)
        finally:
            self._closed = True
            self._inbox.put_nowait(_EOF)


# ---------------------------------------------------------------------------
# HTTP
#
# Routes are opened with salus://http/open|close {method, path}. The framework matches
# incoming requests against them and forwards each as an http://... message whose meta is
#   {id, method, path, route, params, query_string, headers: [[name, value], ...]}
# with the raw request body as payload body. The reply goes to the same frontend on
# http://response with meta {id, status, headers} and the response body.
# ---------------------------------------------------------------------------

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _header_pairs(headers) -> list[tuple[str, str]]:
    if headers is None:
        return []
    items = headers.items() if isinstance(headers, dict) else headers
    return [(str(name), str(value)) for name, value in items]


@dataclass(frozen=True)
class Request:
    frontend_id: int
    method: str
    path: str
    route: str
    params: dict[str, str]
    query_string: str
    headers: tuple[tuple[str, str], ...]
    body: Payload
    request_id: int

    @property
    def query_all(self) -> dict[str, list[str]]:
        return parse_qs(self.query_string, keep_blank_values=True)

    @property
    def query(self) -> dict[str, str]:
        return {name: values[0] for name, values in self.query_all.items()}

    def header(self, name: str, default: str | None = None) -> str | None:
        """First value of a header, matched case-insensitively."""
        name = name.lower()
        for header_name, value in self.headers:
            if header_name.lower() == name:
                return value
        return default


@dataclass
class Response:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: Sendable = b""

    @classmethod
    def text(cls, text: str, status: int = 200, headers=None) -> Response:
        return cls(status, [("content-type", "text/plain; charset=utf-8"), *_header_pairs(headers)], text)

    @classmethod
    def json(cls, value, status: int = 200, headers=None) -> Response:
        return cls(status, [("content-type", "application/json"), *_header_pairs(headers)], _dumps(value))

    @classmethod
    def bytes(cls, data, status: int = 200, content_type: str = "application/octet-stream",
              headers=None) -> Response:
        return cls(status, [("content-type", content_type), *_header_pairs(headers)], data)


class HTTPError(Exception):
    """Raise from a route handler to answer with an error status."""

    def __init__(self, status: int, message: str | None = None, headers=None):
        if not 100 <= status <= 599:
            raise ValueError(f"invalid HTTP status: {status!r}")
        try:
            message = message or HTTPStatus(status).phrase
        except ValueError:
            message = message or "Error"
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message
        self.headers = headers


def _to_response(result) -> Response:
    if isinstance(result, Response):
        return result
    if result is None:
        return Response(204)
    if isinstance(result, str):
        return Response.text(result)
    if isinstance(result, (bytes, bytearray, memoryview, Payload)):
        return Response.bytes(_to_buffer(result))
    if isinstance(result, (dict, list)):
        return Response.json(result)
    raise TypeError(f"route handler returned unsupported type {type(result).__name__}")


def _encode_response(request_id: int, response: Response) -> bytes:
    status = response.status
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        raise ValueError(f"invalid HTTP status: {status!r}")
    meta = {"id": request_id, "status": status,
            "headers": [list(pair) for pair in _header_pairs(response.headers)]}
    return pack_meta(meta, _to_buffer(response.body))


def _parse_request(message: Message, meta: dict, body: bytes) -> Request:
    def expect(key: str, kind, default=None):
        value = meta.get(key, default)
        if not isinstance(value, kind) or isinstance(value, bool):
            raise ValueError(f"request field {key!r} missing or not {kind.__name__}")
        return value

    params = expect("params", dict, {})
    headers = expect("headers", list, [])
    if not all(isinstance(v, str) for v in params.values()):
        raise ValueError("request params must be strings")
    if not all(isinstance(h, list) and len(h) == 2 and all(isinstance(x, str) for x in h) for h in headers):
        raise ValueError("request headers must be [name, value] pairs")
    return Request(
        frontend_id=message.frontend_id,
        method=expect("method", str).upper(),
        path=expect("path", str),
        route=expect("route", str),
        params=params,
        query_string=expect("query_string", str, ""),
        headers=tuple((name, value) for name, value in headers),
        body=Payload(body),
        request_id=expect("id", int),
    )


async def _invoke(handler: Callable, arg):
    if inspect.iscoroutinefunction(handler):
        return await handler(arg)
    return await asyncio.to_thread(handler, arg)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

HANDLER_SHUTDOWN_TIMEOUT = 5.0
REQUEST_TIMEOUT = 5.0


class SalusError(Exception):
    """The framework rejected a salus:// request, or it could not be completed."""


class SalusTimeout(SalusError, TimeoutError):
    """The framework did not answer a salus:// request in time."""


@dataclass(frozen=True)
class SalusResponse:
    meta: dict
    body: Payload


@dataclass(frozen=True)
class ChannelMessage:
    """A message received on a registered ws:// channel."""
    sender: int
    channel: str
    payload: Payload
    message_id: int = 0


Handler = Callable[[ChannelMessage], Awaitable[None] | None]

_current_message: contextvars.ContextVar[ChannelMessage | None] = contextvars.ContextVar(
    "salus_current_message", default=None)


def _channel_name(name: str) -> str:
    if name.startswith(WS_PREFIX):
        name = name[len(WS_PREFIX):]
    if not name or "://" in name:
        raise ValueError(f"invalid channel name: {name!r}")
    return name


class Channel:
    """A ws:// channel. With a handler, messages are dispatched to it; without one,
    iterate it with `async for` to receive messages one at a time."""

    def __init__(self, plugin: Plugin, name: str, handler: Handler | None):
        self._plugin = plugin
        self.name = name
        self._handler = handler
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, data: Sendable, to: int | None = None) -> None:
        await self._plugin.send(data, channel=self.name, to=to)

    def send_blocking(self, data: Sendable, to: int | None = None) -> None:
        self._plugin.send_blocking(data, channel=self.name, to=to)

    def close(self) -> None:
        self._plugin._remove_channel(self)

    def __aiter__(self) -> AsyncIterator[ChannelMessage]:
        if self._handler is not None:
            raise TypeError(f"channel {self.name!r} has a handler and cannot be iterated")
        return self._messages()

    async def _messages(self) -> AsyncIterator[ChannelMessage]:
        while True:
            item = await self._queue.get()
            if item is _EOF:
                self._queue.put_nowait(_EOF)
                return
            yield item


class Route:
    """An HTTP route opened with the framework."""

    def __init__(self, plugin: Plugin, method: str, path: str, handler: Callable):
        self._plugin = plugin
        self.method = method
        self.path = path
        self._handler = handler
        self._open = False

    async def close(self) -> None:
        """Ask the framework to stop forwarding requests for this route. Idempotent."""
        await self._plugin._close_route(self)


class Plugin:
    def __init__(self, socket_path: str | None = None):
        _setup_logging()

        if socket_path is None:
            if len(sys.argv) == 1:
                logger.error("Backend Plugin started without given socket file path! FIX: Pass the socket file path as first argument.")
                sys.exit(1)
            socket_path = sys.argv[1]

        logger.info(f"Passed socket-path: {socket_path}")
        self._socket_path = socket_path
        self._connection: Connection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channels: dict[str, Channel] = {}
        self._handler_tasks: set[asyncio.Task] = set()
        self._routes: dict[tuple[str, str], Route] = {}
        self._request_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._frontends: set[int] = set()
        self._frontend_components: dict[int, str] = {}
        self._attached_handlers: list[Callable] = []
        self._detached_handlers: list[Callable] = []
        self._unclaimed: asyncio.Queue = asyncio.Queue()
        self._unclaimed_consumer = False

    # -- channels ----------------------------------------------------------

    def channel(self, name: str):
        """Decorator: register `handler(msg)` for ws://<name>. Handlers run concurrently,
        one task per message; plain `def` handlers run in a worker thread."""
        def register(handler: Handler) -> Handler:
            self.add_channel(name, handler)
            return handler
        return register

    def add_channel(self, name: str, handler: Handler | None = None) -> Channel:
        name = _channel_name(name)
        if name in self._channels:
            raise ValueError(f"channel {name!r} is already registered")
        channel = self._channels[name] = Channel(self, name, handler)
        return channel

    def remove_channel(self, name: str) -> None:
        self._remove_channel(self._channels[_channel_name(name)])

    def _remove_channel(self, channel: Channel) -> None:
        if self._channels.get(channel.name) is channel:
            del self._channels[channel.name]
            channel._queue.put_nowait(_EOF)

    # -- sending -----------------------------------------------------------

    async def send(self, data: Sendable, channel: str | None = None, to: int | None = None) -> None:
        """Send on a ws:// channel.

        Inside a channel handler, `channel` defaults to the channel being handled and `to`
        to the sender of the message being handled. Elsewhere `channel` is required and
        `to` defaults to ALL. Pass `to=salus.ALL` to broadcast from inside a handler."""
        wire_channel, target = self._resolve_target(channel, to)
        await self._require_connection().send_message(target, wire_channel, data)

    def send_blocking(self, data: Sendable, channel: str | None = None, to: int | None = None) -> None:
        """Like send(), for plain `def` handlers running in worker threads."""
        wire_channel, target = self._resolve_target(channel, to)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            raise RuntimeError("send_blocking() would deadlock the event loop; use `await send()` instead")
        connection = self._require_connection()
        asyncio.run_coroutine_threadsafe(
            connection.send_message(target, wire_channel, data), self._loop).result()

    async def send_message(self, frontend_id: int, channel: str, data: Sendable) -> None:
        """Low level: send on any logical channel, with an explicit frontend id."""
        await self._require_connection().send_message(frontend_id, channel, data)

    def _resolve_target(self, channel: str | None, to: int | None) -> tuple[str, int]:
        current = _current_message.get()
        if channel is None:
            if current is None:
                raise ValueError("channel is required outside of a channel handler")
            name = current.channel
        else:
            name = _channel_name(channel)
        if to is None:
            to = ALL if current is None else current.sender
        if not 0 <= to <= MAX_ID:
            raise ValueError(f"frontend id out of range: {to}")
        return WS_PREFIX + name, to

    # -- HTTP routes -------------------------------------------------------

    def route(self, method: str, path: str):
        """Decorator: serve `method path` with `handler(request)`. Registered with the
        framework when the plugin starts. Use add_route() once the plugin is running.

        A handler returns a Response, or a str / bytes / dict / list / None, and may
        raise HTTPError. Plain `def` handlers run in a worker thread."""
        def register(handler: Callable) -> Callable:
            if self._connection is not None:
                raise RuntimeError("@route can only be used before run(); use `await add_route()` instead")
            self._register_route(method, path, handler)
            return handler
        return register

    async def add_route(self, method: str, path: str, handler: Callable) -> Route:
        route = self._register_route(method, path, handler)
        try:
            await self._open_route(route)
        except BaseException:
            del self._routes[(route.method, route.path)]
            raise
        return route

    async def remove_route(self, method: str, path: str) -> None:
        await self._routes[self._route_key(method, path)].close()

    @staticmethod
    def _route_key(method: str, path: str) -> tuple[str, str]:
        method = method.upper()
        if method not in _HTTP_METHODS:
            raise ValueError(f"unsupported HTTP method: {method!r}")
        if not path.startswith("/"):
            raise ValueError(f"route path must start with '/': {path!r}")
        return method, path

    def _register_route(self, method: str, path: str, handler: Callable) -> Route:
        key = self._route_key(method, path)
        if key in self._routes:
            raise ValueError(f"route {key[0]} {key[1]} is already registered")
        route = self._routes[key] = Route(self, *key, handler)
        return route

    async def _open_route(self, route: Route) -> None:
        await self.request("http/open", {"method": route.method, "path": route.path})
        route._open = True

    async def _close_route(self, route: Route) -> None:
        if self._routes.get((route.method, route.path)) is not route:
            return
        await self.request("http/close", {"method": route.method, "path": route.path})
        route._open = False
        del self._routes[(route.method, route.path)]

    async def _open_static_routes(self) -> None:
        for route in list(self._routes.values()):
            if not route._open:
                await self._open_route(route)

    def _route_http(self, message: Message) -> bool:
        try:
            meta, body = unpack_meta(message.payload.as_bytes())
        except ProtocolError as e:
            logger.warning("Dropping malformed %s message: %s", message.channel, e)
            return True
        if "method" not in meta:
            return False

        try:
            request = _parse_request(message, meta, body)
        except ValueError as e:
            logger.warning("Bad HTTP request from frontend %d: %s", message.frontend_id, e)
            if isinstance(meta.get("id"), int):
                self._track(asyncio.create_task(self._send_response(
                    message.frontend_id, meta["id"], Response.text("Bad Request", 400))))
            return True

        route = self._routes.get((request.method, request.route))
        if route is None:
            logger.warning("No handler for %s %s (route %s)", request.method, request.path, request.route)
            self._track(asyncio.create_task(self._send_response(
                request.frontend_id, request.request_id, Response.text("Not Found", 404))))
        else:
            self._track(asyncio.create_task(self._handle_http(route, request)))
        return True

    async def _handle_http(self, route: Route, request: Request) -> None:
        try:
            payload = _encode_response(
                request.request_id, _to_response(await _invoke(route._handler, request)))
        except HTTPError as e:
            payload = _encode_response(request.request_id, Response.text(e.message, e.status, e.headers))
        except Exception:
            logger.exception("Handler for %s %s failed", route.method, route.path)
            payload = _encode_response(request.request_id, Response.text("Internal Server Error", 500))
        await self._send_payload(request.frontend_id, request.request_id, payload)

    async def _send_response(self, frontend_id: int, request_id: int, response: Response) -> None:
        await self._send_payload(frontend_id, request_id, _encode_response(request_id, response))

    async def _send_payload(self, frontend_id: int, request_id: int, payload: bytes) -> None:
        try:
            await self._require_connection().send_message(frontend_id, HTTP_RESPONSE_CHANNEL, payload)
        except ConnectionClosed:
            logger.warning("Connection closed before the response to request %d could be sent", request_id)
        except Exception:
            logger.exception("Could not send response to request %d", request_id)

    # -- frontends ---------------------------------------------------------

    @property
    def frontends(self) -> frozenset[int]:
        """Ids of the frontends currently bound to this backend."""
        return frozenset(self._frontends)

    def frontend_component(self, frontend_id: int) -> str | None:
        """Name of the frontend component (`component-name` in the manifest) a bound frontend runs,
        or None if unknown. Known as soon as the frontend attached, so it works in attach handlers."""
        return self._frontend_components.get(frontend_id)

    def on_frontend_attached(self, handler: Callable[[int], Awaitable[None] | None]):
        """Decorator: call `handler(frontend_id)` whenever a frontend binds to this backend."""
        self._attached_handlers.append(handler)
        return handler

    def on_frontend_detached(self, handler: Callable[[int], Awaitable[None] | None]):
        """Decorator: call `handler(frontend_id)` whenever a frontend unbinds."""
        self._detached_handlers.append(handler)
        return handler

    # -- salus:// requests -------------------------------------------------

    async def request(self, topic: str, params: dict | None = None,
                      body: Sendable = b"", timeout: float | None = REQUEST_TIMEOUT) -> SalusResponse:
        """Send a request to the framework on salus://<topic> and wait for its reply.

        Raises SalusError if the framework rejects it and SalusTimeout if it does not answer."""
        params = dict(params or {})
        if "id" in params:
            raise ValueError("'id' is reserved for request correlation")
        connection = self._require_connection()

        request_id = next(self._request_ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await connection.send_message(
                NO_FRONTEND, SALUS_PREFIX + topic,
                pack_meta({"id": request_id, **params}, _to_buffer(body)))
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise SalusTimeout(f"no reply to salus://{topic} within {timeout}s") from None
        finally:
            self._pending.pop(request_id, None)

    def _route_salus(self, message: Message) -> bool:
        topic = message.channel[len(SALUS_PREFIX):]
        try:
            meta, body = unpack_meta(message.payload.as_bytes())
        except ProtocolError as e:
            logger.warning("Dropping malformed %s message: %s", message.channel, e)
            return True

        if topic == "frontend/attached":
            self._frontends.add(message.frontend_id)
            if isinstance(meta.get("component"), str):
                self._frontend_components[message.frontend_id] = meta["component"]
            for handler in self._attached_handlers:
                self._spawn(handler, message.frontend_id, f"frontend attached ({message.frontend_id})")
            return True
        if topic == "frontend/detached":
            self._frontends.discard(message.frontend_id)
            self._frontend_components.pop(message.frontend_id, None)
            for handler in self._detached_handlers:
                self._spawn(handler, message.frontend_id, f"frontend detached ({message.frontend_id})")
            return True

        if "id" in meta and "ok" in meta:
            future = self._pending.get(meta["id"]) if isinstance(meta["id"], int) else None
            if future is None or future.done():
                logger.warning("Unexpected reply %r on %s (request already timed out?)",
                               meta["id"], message.channel)
            elif meta["ok"] is True:
                future.set_result(SalusResponse(meta, Payload(body)))
            else:
                future.set_exception(SalusError(str(meta.get("error", "request failed"))))
            return True
        return False

    # -- receiving ---------------------------------------------------------

    def messages(self) -> AsyncIterator[Message]:
        """Low level: every message that no registered channel claims (including
        other schemes), as raw frames' worth of data. Messages arriving while nobody
        is iterating are dropped."""
        self._unclaimed_consumer = True
        return self._unclaimed_messages()

    async def _unclaimed_messages(self) -> AsyncIterator[Message]:
        while True:
            item = await self._unclaimed.get()
            if item is _EOF:
                self._unclaimed.put_nowait(_EOF)
                return
            yield item

    async def _dispatch(self, connection: Connection) -> None:
        try:
            async for message in connection.messages():
                self._route(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionClosed("connection closed before the reply arrived"))
            for channel in list(self._channels.values()):
                channel._queue.put_nowait(_EOF)
            self._unclaimed.put_nowait(_EOF)

    def _route(self, message: Message) -> None:
        if message.channel.startswith(WS_PREFIX):
            channel = self._channels.get(message.channel[len(WS_PREFIX):])
            if channel is not None:
                routed = ChannelMessage(
                    message.frontend_id, channel.name, message.payload, message.message_id)
                if channel._handler is None:
                    channel._queue.put_nowait(routed)
                else:
                    self._spawn(channel._handler, routed, f"channel {channel.name!r}", routed)
                return
        elif message.channel.startswith(SALUS_PREFIX) and self._route_salus(message):
            return
        elif message.channel.startswith(HTTP_PREFIX) and self._route_http(message):
            return

        if self._unclaimed_consumer:
            self._unclaimed.put_nowait(message)
        else:
            logger.warning("No handler for channel %r (from frontend %d); dropping message",
                           message.channel, message.frontend_id)

    def _spawn(self, handler: Callable, arg, description: str,
               message: ChannelMessage | None = None) -> None:
        self._track(asyncio.create_task(self._run_handler(handler, arg, description, message)))

    def _track(self, task: asyncio.Task) -> None:
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _run_handler(self, handler: Callable, arg, description: str,
                           message: ChannelMessage | None) -> None:
        if message is not None:
            _current_message.set(message)
        try:
            await _invoke(handler, arg)
        except Exception:
            logger.exception("Handler for %s failed", description)

    # -- lifecycle ---------------------------------------------------------

    def run(self, main: Callable[[Plugin], Awaitable[None]] | None = None) -> None:
        """Connect to the framework and serve until it closes the connection.

        `main`, if given, runs concurrently as the plugin's own coroutine."""
        try:
            asyncio.run(self._run(main))
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.exception("Plugin failed")
            sys.exit(1)

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("Plugin is not connected; use this from inside run()")
        return self._connection

    async def _run(self, main) -> None:
        self._loop = asyncio.get_running_loop()
        connection = self._connection = await Connection.open(self._socket_path)
        logger.info("Connected to framework")

        dispatch = asyncio.create_task(self._dispatch(connection))
        closed = asyncio.create_task(connection.wait_closed())
        main_task = None
        try:
            await self._open_static_routes()
            if main is not None:
                main_task = asyncio.create_task(main(self))
            pending = {t for t in (closed, main_task) if t is not None}
            while closed in pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if main_task in done:
                    main_task.result()
            closed.result()
            logger.info("Framework closed the connection")

            await dispatch
            await self._finish_handlers()
        finally:
            tasks = [t for t in (closed, main_task, dispatch, *self._handler_tasks) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await connection.close()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._connection = None

    async def _finish_handlers(self) -> None:
        tasks = list(self._handler_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=HANDLER_SHUTDOWN_TIMEOUT)
        if pending:
            logger.warning("Cancelling %d handler(s) still running %.0fs after shutdown",
                           len(pending), HANDLER_SHUTDOWN_TIMEOUT)
