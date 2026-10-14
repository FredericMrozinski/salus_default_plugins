import asyncio
import json
import os
import shutil
import tempfile
import unittest

import salus_sdk as sdk
from salus_sdk import Frame, ProtocolError


class FrameCodecTest(unittest.TestCase):
    def test_golden_bytes(self):
        frame = Frame(frontend_id=1, message_id=2, channel="ws://a", payload=b"hi")
        expected = bytes.fromhex(
            "01000000"        # frontend_id
            "02000000"        # message_id
            "00"              # flags
            "0600"            # channel_len
            "77733a2f2f61"    # "ws://a"
            "6869"            # "hi"
        )
        self.assertEqual(sdk.encode_frame(frame), expected)
        self.assertEqual(sdk.decode_frame(expected), frame)

    def test_pack_adds_length_prefix(self):
        frame = Frame(1, 2, "ws://a", b"hi")
        packed = sdk.pack_frame(frame)
        body = sdk.encode_frame(frame)
        self.assertEqual(packed[:4], len(body).to_bytes(4, "little"))
        self.assertEqual(packed[4:], body)

    def test_roundtrip_edge_values(self):
        for frame in (
            Frame(0, 0, "", b""),
            Frame(sdk.MAX_ID, sdk.MAX_ID, "ws://ünï/çödé", bytes(range(256)), sdk.FLAG_MORE_FRAGMENTS),
            Frame(7, 9, "x" * sdk.MAX_CHANNEL_LEN, b"\x00" * 1000),
        ):
            self.assertEqual(sdk.decode_frame(sdk.encode_frame(frame)), frame)

    def test_more_fragments_property(self):
        self.assertTrue(Frame(1, 1, "c", flags=sdk.FLAG_MORE_FRAGMENTS).more_fragments)
        self.assertFalse(Frame(1, 1, "c").more_fragments)

    def test_decode_rejects_short_frame(self):
        with self.assertRaises(ProtocolError):
            sdk.decode_frame(b"\x00" * 10)

    def test_decode_rejects_channel_past_end(self):
        body = bytearray(sdk.encode_frame(Frame(1, 1, "abc")))
        body[9:11] = (200).to_bytes(2, "little")
        with self.assertRaises(ProtocolError):
            sdk.decode_frame(bytes(body))

    def test_decode_rejects_invalid_utf8_channel(self):
        body = bytearray(sdk.encode_frame(Frame(1, 1, "ab")))
        body[11:13] = b"\xff\xfe"
        with self.assertRaises(ProtocolError):
            sdk.decode_frame(bytes(body))

    def test_unknown_flags_rejected(self):
        body = bytearray(sdk.encode_frame(Frame(1, 1, "c")))
        body[8] = 0x02
        with self.assertRaises(ProtocolError):
            sdk.decode_frame(bytes(body))
        with self.assertRaises(ProtocolError):
            sdk.encode_frame(Frame(1, 1, "c", flags=0x80))

    def test_encode_rejects_out_of_range(self):
        for frame in (
            Frame(-1, 0, "c"),
            Frame(0, sdk.MAX_ID + 1, "c"),
            Frame(0, 0, "x" * (sdk.MAX_CHANNEL_LEN + 1)),
        ):
            with self.assertRaises(ProtocolError):
                sdk.encode_frame(frame)

    def test_size_limit(self):
        header_and_channel = 11 + len("c")
        ok = Frame(1, 1, "c", b"\x00" * (sdk.MAX_FRAME_SIZE - header_and_channel))
        self.assertEqual(len(sdk.encode_frame(ok)), sdk.MAX_FRAME_SIZE)
        too_big = Frame(1, 1, "c", b"\x00" * (sdk.MAX_FRAME_SIZE - header_and_channel + 1))
        with self.assertRaises(ProtocolError):
            sdk.encode_frame(too_big)

    def test_check_frame_length(self):
        self.assertEqual(sdk.check_frame_length(sdk.MAX_FRAME_SIZE), sdk.MAX_FRAME_SIZE)
        with self.assertRaises(ProtocolError):
            sdk.check_frame_length(sdk.MAX_FRAME_SIZE + 1)


class PayloadTest(unittest.TestCase):
    def test_scalar_golden_bytes(self):
        P = sdk.Payload
        self.assertEqual(P.u8(255).as_bytes(), b"\xff")
        self.assertEqual(P.u32(1).as_bytes(), b"\x01\x00\x00\x00")
        self.assertEqual(P.i16(-2).as_bytes(), b"\xfe\xff")
        self.assertEqual(P.u64(2**64 - 1).as_bytes(), b"\xff" * 8)
        self.assertEqual(P.f64(1.0).as_bytes(), bytes.fromhex("000000000000f03f"))
        self.assertEqual(P.f32(1.0).as_bytes(), bytes.fromhex("0000803f"))
        self.assertEqual(P.bool(True).as_bytes(), b"\x01")
        self.assertEqual(P.bool(False).as_bytes(), b"\x00")

    def test_scalar_roundtrip(self):
        P = sdk.Payload
        cases = {
            "u8": (0, 255), "i8": (-128, 127), "u16": (0, 65535), "i16": (-32768, 32767),
            "u32": (0, 2**32 - 1), "i32": (-2**31, 2**31 - 1),
            "u64": (0, 2**64 - 1), "i64": (-2**63, 2**63 - 1),
            "f32": (0.5, -2.25), "f64": (3.141592653589793, -1e300), "bool": (True, False),
        }
        for kind, values in cases.items():
            for value in values:
                self.assertEqual(getattr(P, kind)(value).__getattribute__(f"as_{kind}")(), value, kind)

    def test_scalar_wrong_length(self):
        for payload, accessor in ((b"", "as_u8"), (b"\x00\x00", "as_u8"), (b"\x00" * 7, "as_f64"),
                                  (b"\x00" * 9, "as_i64"), (b"", "as_bool")):
            with self.assertRaises(sdk.PayloadError, msg=accessor):
                getattr(sdk.Payload(payload), accessor)()

    def test_bool_rejects_other_bytes(self):
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload(b"\x02").as_bool()

    def test_scalar_encode_errors(self):
        P = sdk.Payload
        for build in (lambda: P.u8(256), lambda: P.u8(-1), lambda: P.i8(128), lambda: P.u8(1.5),
                      lambda: P.u32("1"), lambda: P.f32(1e40), lambda: P.f64("x")):
            with self.assertRaises(sdk.PayloadError):
                build()

    def test_str(self):
        p = sdk.Payload.str("héllo ✓")
        self.assertEqual(p.as_bytes(), "héllo ✓".encode())
        self.assertEqual(p.as_str(), "héllo ✓")
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload(b"\xff\xfe").as_str()
        with self.assertRaises(TypeError):
            sdk.Payload.str(5)

    def test_json(self):
        doc = {"a": [1, 2.5, None, True], "b": "ü"}
        p = sdk.Payload.json(doc)
        self.assertEqual(p.as_bytes(), '{"a":[1,2.5,null,true],"b":"ü"}'.encode())
        self.assertEqual(p.as_json(), doc)
        for bad in (b"{", b"NaN", b"Infinity", b"\xff"):
            with self.assertRaises(sdk.PayloadError):
                sdk.Payload(bad).as_json()
        for unserializable in (float("nan"), object(), {1, 2}):
            with self.assertRaises(sdk.PayloadError):
                sdk.Payload.json(unserializable)

    def test_builder_golden_bytes(self):
        p = sdk.Payload.builder().u32(1).str("ab").bytes(b"\x09").bool(True).raw(b"!!").build()
        self.assertEqual(p.as_bytes(), bytes.fromhex(
            "01000000" "02000000" "6162" "01000000" "09" "01" "2121"))

    def test_builder_reader_roundtrip(self):
        p = (sdk.Payload.builder()
             .u8(7).i64(-5).f64(36.6).f32(0.5).str("ünï").bytes(b"\x00\x01").json({"k": [1]}).bool(False)
             .build())
        r = p.reader()
        self.assertEqual((r.u8(), r.i64(), r.f64(), r.f32()), (7, -5, 36.6, 0.5))
        self.assertEqual((r.str(), r.bytes(), r.json(), r.bool()), ("ünï", b"\x00\x01", {"k": [1]}, False))
        self.assertTrue(r.at_end)
        r.expect_end()

    def test_reader_rest_and_remaining(self):
        r = sdk.Payload(b"\x01\x00abcd").reader()
        self.assertEqual(r.u16(), 1)
        self.assertEqual(r.remaining, 4)
        self.assertEqual(r.raw(1), b"a")
        self.assertEqual(r.rest(), b"bcd")
        self.assertTrue(r.at_end)

    def test_reader_errors(self):
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload(b"\x00").reader().u16()
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload(b"\x05\x00\x00\x00ab").reader().str()
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload(b"\x01\x00\x00\x00\xff").reader().str()
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload(b"\x02").reader().bool()
        r = sdk.Payload(b"\x00\x00").reader()
        r.u8()
        with self.assertRaises(sdk.PayloadError):
            r.expect_end()

    def test_failed_read_does_not_advance(self):
        r = sdk.Payload(b"\x01").reader()
        with self.assertRaises(sdk.PayloadError):
            r.u32()
        self.assertEqual(r.u8(), 1)

    def test_builder_errors(self):
        with self.assertRaises(sdk.PayloadError):
            sdk.Payload.builder().u8(300)
        with self.assertRaises(TypeError):
            sdk.Payload.builder().str(b"bytes")

    def test_value_semantics(self):
        p = sdk.Payload(b"abc")
        self.assertEqual(p, sdk.Payload(bytearray(b"abc")))
        self.assertEqual(p, b"abc")
        self.assertEqual(b"abc", p)
        self.assertNotEqual(p, b"abd")
        self.assertNotEqual(p, "abc")
        self.assertEqual(hash(p), hash(b"abc"))
        self.assertEqual(len(p), 3)
        self.assertEqual(bytes(p), b"abc")
        self.assertEqual(len(sdk.Payload()), 0)
        self.assertIn("3 bytes", repr(p))
        self.assertIn("...", repr(sdk.Payload(b"x" * 100)))

    def test_constructor_does_not_copy_bytes(self):
        data = b"x" * 10
        self.assertIs(sdk.Payload(data).as_bytes(), data)


class ReassemblerTest(unittest.TestCase):
    def setUp(self):
        self.r = sdk._Reassembler(max_message_size=10)

    def frag(self, mid, payload, more, channel="c", fe=1):
        return Frame(fe, mid, channel, payload, sdk.FLAG_MORE_FRAGMENTS if more else 0)

    def test_single_frame_passes_through(self):
        msg = self.r.feed(self.frag(1, b"abc", False))
        self.assertEqual(msg, sdk.Message(1, "c", b"abc", 1))

    def test_interleaved_messages(self):
        feed = self.r.feed
        self.assertIsNone(feed(self.frag(1, b"aa", True)))
        self.assertIsNone(feed(self.frag(2, b"xx", True)))
        self.assertEqual(feed(self.frag(3, b"solo", False)).payload, b"solo")
        self.assertEqual(feed(self.frag(2, b"yy", False)).payload, b"xxyy")
        self.assertEqual(feed(self.frag(1, b"bb", False)).payload, b"aabb")

    def test_same_message_id_from_different_frontends_is_separate(self):
        self.r.feed(self.frag(1, b"a", True, fe=1))
        self.r.feed(self.frag(1, b"b", True, fe=2))
        self.assertEqual(self.r.feed(self.frag(1, b"c", False, fe=1)).payload, b"ac")
        self.assertEqual(self.r.feed(self.frag(1, b"d", False, fe=2)).payload, b"bd")

    def test_channel_switch_rejected_and_rest_discarded(self):
        self.r.feed(self.frag(1, b"a", True, channel="c"))
        with self.assertRaises(ProtocolError):
            self.r.feed(self.frag(1, b"b", True, channel="other"))
        self.assertIsNone(self.r.feed(self.frag(1, b"c", False, channel="other")))
        self.assertEqual(self.r.feed(self.frag(1, b"ok", False)).payload, b"ok")

    def test_oversized_message_dropped_until_last_fragment(self):
        self.r.feed(self.frag(1, b"123456", True))
        with self.assertRaises(ProtocolError):
            self.r.feed(self.frag(1, b"123456", True))
        self.assertIsNone(self.r.feed(self.frag(1, b"x", True)))
        self.assertIsNone(self.r.feed(self.frag(1, b"x", False)))
        self.assertEqual(self.r.feed(self.frag(1, b"ok", False)).payload, b"ok")


class FakeFramework:
    """Stands in for the framework: listens on a Unix socket and speaks the wire format."""

    async def start(self):
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "s")
        self._connected = asyncio.Event()
        self._server = await asyncio.start_unix_server(self._on_connect, self.path)

    async def _on_connect(self, reader, writer):
        self.reader, self.writer = reader, writer
        self._connected.set()

    async def accept(self):
        await asyncio.wait_for(self._connected.wait(), 5)

    def send(self, frame):
        self.writer.write(sdk.pack_frame(frame))

    def send_raw(self, data):
        self.writer.write(data)

    async def read_frame(self):
        (length,) = sdk.LENGTH_PREFIX.unpack(await asyncio.wait_for(self.reader.readexactly(4), 5))
        return sdk.decode_frame(await asyncio.wait_for(self.reader.readexactly(length), 5))

    async def stop(self):
        if hasattr(self, "writer"):
            self.writer.close()
        self._server.close()
        await self._server.wait_closed()
        shutil.rmtree(self._dir, ignore_errors=True)


class ConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fw = FakeFramework()
        await self.fw.start()
        self.addAsyncCleanup(self.fw.stop)

    async def connect(self, **kwargs):
        conn = await sdk.Connection.open(self.fw.path, **kwargs)
        self.addAsyncCleanup(conn.close)
        await self.fw.accept()
        return conn

    async def next_message(self, conn):
        return await asyncio.wait_for(anext(conn.messages()), 5)

    async def test_receive_message(self):
        conn = await self.connect()
        self.fw.send(Frame(4, 9, "ws://chat", b"hello"))
        self.assertEqual(await self.next_message(conn), sdk.Message(4, "ws://chat", b"hello", 9))

    async def test_receive_fragmented_and_interleaved(self):
        conn = await self.connect()
        more = sdk.FLAG_MORE_FRAGMENTS
        self.fw.send(Frame(1, 1, "ws://a", b"AA", more))
        self.fw.send(Frame(1, 2, "ws://b", b"B", 0))
        self.fw.send(Frame(1, 1, "ws://a", b"BB", 0))
        self.assertEqual((await self.next_message(conn)).payload, b"B")
        self.assertEqual((await self.next_message(conn)).payload, b"AABB")

    async def test_send_message(self):
        conn = await self.connect()
        await conn.send_message(7, "ws://out", b"data")
        frame = await self.fw.read_frame()
        self.assertEqual((frame.frontend_id, frame.channel, frame.payload, frame.flags),
                         (7, "ws://out", b"data", 0))

    async def test_send_accepts_payload_str_and_buffers(self):
        conn = await self.connect()
        for data, expected in (
            (sdk.Payload.f64(1.0), bytes.fromhex("000000000000f03f")),
            ("héllo", "héllo".encode()),
            (bytearray(b"ba"), b"ba"),
            (memoryview(b"mv"), b"mv"),
        ):
            await conn.send_message(1, "c", data)
            self.assertEqual((await self.fw.read_frame()).payload, expected)

    async def test_received_payload_is_typed(self):
        conn = await self.connect()
        self.fw.send(Frame(1, 1, "c", sdk.Payload.builder().u32(5).str("x").build().as_bytes()))
        r = (await self.next_message(conn)).payload.reader()
        self.assertEqual((r.u32(), r.str()), (5, "x"))

    async def test_send_empty_message(self):
        conn = await self.connect()
        await conn.send_message(7, "ws://out", b"")
        frame = await self.fw.read_frame()
        self.assertEqual((frame.payload, frame.flags), (b"", 0))

    async def test_send_fragments_large_message(self):
        conn = await self.connect(max_fragment_payload=4)
        await conn.send_message(3, "ws://big", b"0123456789")
        frames = [await self.fw.read_frame() for _ in range(3)]
        self.assertEqual([f.payload for f in frames], [b"0123", b"4567", b"89"])
        self.assertEqual([f.more_fragments for f in frames], [True, True, False])
        self.assertEqual(len({f.message_id for f in frames}), 1)
        self.assertTrue(all(f.channel == "ws://big" and f.frontend_id == 3 for f in frames))

    async def test_exact_multiple_of_fragment_size(self):
        conn = await self.connect(max_fragment_payload=4)
        await conn.send_message(3, "c", b"01234567")
        frames = [await self.fw.read_frame() for _ in range(2)]
        self.assertEqual([f.more_fragments for f in frames], [True, False])

    async def test_concurrent_sends_reassemble_correctly(self):
        conn = await self.connect(max_fragment_payload=3)
        a, b = b"a" * 20, b"b" * 20
        await asyncio.gather(conn.send_message(1, "x", a), conn.send_message(1, "y", b))
        reassembler = sdk._Reassembler(1000)
        done = {}
        while len(done) < 2:
            msg = reassembler.feed(await self.fw.read_frame())
            if msg:
                done[msg.channel] = msg.payload
        self.assertEqual(done, {"x": a, "y": b})

    async def test_concurrent_multi_slice_frames_do_not_interleave(self):
        conn = await self.connect()
        size = 3 * sdk._WRITE_SLICE + 123
        a, b = b"a" * size, b"b" * size
        send = asyncio.gather(conn.send_message(1, "x", a), conn.send_message(1, "y", b))
        frames = [await self.fw.read_frame() for _ in range(2)]
        await send
        self.assertEqual({f.channel: f.payload for f in frames}, {"x": a, "y": b})

    async def test_cancelled_send_does_not_tear_the_stream(self):
        conn = await self.connect()
        size = 20 * sdk._WRITE_SLICE
        first = asyncio.create_task(conn.send_message(1, "x", b"a" * size))
        await asyncio.sleep(0.05)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(conn.send_message(1, "y", b"after"))
        frames = [await self.fw.read_frame() for _ in range(2)]
        await second
        self.assertEqual([(f.channel, len(f.payload)) for f in frames], [("x", size), ("y", 5)])

    async def test_message_ids_differ_between_messages(self):
        conn = await self.connect()
        await conn.send_message(1, "c", b"1")
        await conn.send_message(1, "c", b"2")
        ids = [(await self.fw.read_frame()).message_id for _ in range(2)]
        self.assertNotEqual(ids[0], ids[1])

    async def test_message_id_wraps_and_skips_in_flight(self):
        conn = await self.connect()
        conn._next_message_id = sdk.MAX_ID
        conn._outgoing_ids.add(0)
        self.assertEqual(conn._allocate_message_id(), sdk.MAX_ID)
        self.assertEqual(conn._allocate_message_id(), 1)

    async def test_clean_close_ends_iteration(self):
        conn = await self.connect()
        self.fw.send(Frame(1, 1, "c", b"last"))
        self.fw.writer.close()
        received = [m async for m in conn.messages()]
        self.assertEqual([m.payload for m in received], [b"last"])
        await conn.wait_closed()

    async def test_invalid_frame_is_skipped(self):
        conn = await self.connect()
        bad = bytearray(sdk.encode_frame(Frame(1, 1, "c")))
        bad[8] = 0x80
        self.fw.send_raw(sdk.LENGTH_PREFIX.pack(len(bad)) + bytes(bad))
        self.fw.send(Frame(1, 2, "c", b"good"))
        with self.assertLogs("SALUS SDK", "WARNING"):
            self.assertEqual((await self.next_message(conn)).payload, b"good")

    async def test_oversized_length_prefix_is_fatal(self):
        conn = await self.connect()
        self.fw.send_raw(sdk.LENGTH_PREFIX.pack(sdk.MAX_FRAME_SIZE + 1))
        with self.assertRaises(ProtocolError):
            await asyncio.wait_for(conn.wait_closed(), 5)
        with self.assertRaises(sdk.ConnectionClosed):
            await conn.send_message(1, "c", b"x")

    async def test_close_mid_frame_is_an_error(self):
        conn = await self.connect()
        self.fw.send_raw(sdk.LENGTH_PREFIX.pack(100) + b"short")
        self.fw.writer.close()
        with self.assertRaises(sdk.ConnectionClosed):
            await asyncio.wait_for(conn.wait_closed(), 5)

    async def test_send_after_close_raises(self):
        conn = await self.connect()
        await conn.close()
        with self.assertRaises(sdk.ConnectionClosed):
            await conn.send_message(1, "c", b"x")


class PluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fw = FakeFramework()
        await self.fw.start()
        self.addAsyncCleanup(self.fw.stop)

    async def test_main_echo_and_exit_on_close(self):
        async def main(app):
            async for msg in app.messages():
                await app.send_message(msg.frontend_id, msg.channel, sdk.Payload.str("Echo: " + msg.payload.as_str()))

        plugin = sdk.Plugin(self.fw.path)
        run = asyncio.create_task(plugin._run(main))
        await self.fw.accept()
        self.fw.send(Frame(5, 1, "ws://chat", b"hi"))
        reply = await self.fw.read_frame()
        self.assertEqual((reply.frontend_id, reply.channel, reply.payload), (5, "ws://chat", b"Echo: hi"))
        self.fw.writer.close()
        await asyncio.wait_for(run, 5)

    async def test_main_exception_propagates(self):
        async def main(app):
            raise ValueError("boom")

        plugin = sdk.Plugin(self.fw.path)
        with self.assertRaises(ValueError):
            await asyncio.wait_for(plugin._run(main), 5)

    async def test_main_returning_keeps_plugin_running(self):
        async def main(app):
            return

        plugin = sdk.Plugin(self.fw.path)
        run = asyncio.create_task(plugin._run(main))
        await self.fw.accept()
        await asyncio.sleep(0.1)
        self.assertFalse(run.done())
        self.fw.writer.close()
        await asyncio.wait_for(run, 5)

    async def test_send_requires_connection(self):
        plugin = sdk.Plugin(self.fw.path)
        with self.assertRaises(RuntimeError):
            await plugin.send(b"x", channel="c")
        with self.assertRaises(RuntimeError):
            await plugin.send_message(1, "c", b"x")


class ChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fw = FakeFramework()
        await self.fw.start()
        self.addAsyncCleanup(self.fw.stop)
        self.app = sdk.Plugin(self.fw.path)
        self.run = None
        self.addAsyncCleanup(self._cancel_run)

    async def _cancel_run(self):
        if self.run is not None and not self.run.done():
            self.run.cancel()
            await asyncio.gather(self.run, return_exceptions=True)

    async def start(self, main=None):
        self.run = asyncio.create_task(self.app._run(main))
        await self.fw.accept()

    async def finish(self):
        self.fw.writer.close()
        await asyncio.wait_for(self.run, 10)

    # -- routing and send defaults ----------------------------------------

    async def test_handler_receives_channel_message_and_reply_defaults(self):
        seen = []

        @self.app.channel("chat")
        async def chat(msg):
            seen.append(msg)
            await self.app.send(sdk.Payload.str("pong"))

        await self.start()
        self.fw.send(Frame(4, 9, "ws://chat", b"ping"))
        reply = await self.fw.read_frame()
        self.assertEqual((reply.frontend_id, reply.channel, reply.payload), (4, "ws://chat", b"pong"))
        self.assertEqual(seen, [sdk.ChannelMessage(4, "chat", b"ping", 9)])
        await self.finish()

    async def test_overriding_defaults_inside_handler(self):
        @self.app.channel("a")
        async def a(msg):
            await self.app.send(b"1", to=sdk.ALL)
            await self.app.send(b"2", channel="b")
            await self.app.send(b"3", channel="ws://c", to=77)

        await self.start()
        self.fw.send(Frame(4, 1, "ws://a", b""))
        got = [await self.fw.read_frame() for _ in range(3)]
        self.assertEqual([(f.frontend_id, f.channel, f.payload) for f in got],
                         [(sdk.ALL, "ws://a", b"1"), (4, "ws://b", b"2"), (77, "ws://c", b"3")])
        await self.finish()

    async def test_send_outside_handler(self):
        async def main(app):
            with self.assertRaises(ValueError):
                await app.send(b"x")
            with self.assertRaises(ValueError):
                await app.send(b"x", channel="http://nope")
            with self.assertRaises(ValueError):
                await app.send(b"x", channel="c", to=-1)
            await app.send(b"broadcast", channel="news")
            await app.send(b"direct", channel="news", to=3)

        await self.start(main)
        got = [await self.fw.read_frame() for _ in range(2)]
        self.assertEqual([(f.frontend_id, f.channel, f.payload) for f in got],
                         [(sdk.ALL, "ws://news", b"broadcast"), (3, "ws://news", b"direct")])
        await self.finish()

    async def test_sync_handler_runs_in_thread_with_send_blocking(self):
        import threading
        threads = []

        @self.app.channel("work")
        def work(msg):
            threads.append(threading.current_thread())
            self.app.send_blocking(sdk.Payload.str(msg.payload.as_str().upper()))

        await self.start()
        self.fw.send(Frame(8, 1, "ws://work", b"abc"))
        reply = await self.fw.read_frame()
        self.assertEqual((reply.frontend_id, reply.channel, reply.payload), (8, "ws://work", b"ABC"))
        self.assertIsNot(threads[0], threading.main_thread())
        await self.finish()

    async def test_send_blocking_on_loop_thread_raises(self):
        errors = []

        async def main(app):
            try:
                app.send_blocking(b"x", channel="c")
            except RuntimeError as e:
                errors.append(e)

        await self.start(main)
        await asyncio.sleep(0.1)
        self.assertEqual(len(errors), 1)
        await self.finish()

    async def test_handlers_run_concurrently(self):
        release = asyncio.Event()
        finished = []

        @self.app.channel("c")
        async def handler(msg):
            if msg.payload == b"slow":
                await release.wait()
            else:
                release.set()
            finished.append(msg.payload.as_str())

        await self.start()
        self.fw.send(Frame(1, 1, "ws://c", b"slow"))
        self.fw.send(Frame(1, 2, "ws://c", b"fast"))
        for _ in range(100):
            if len(finished) == 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(finished, ["fast", "slow"])
        await self.finish()

    async def test_handler_exception_is_logged_and_plugin_continues(self):
        @self.app.channel("c")
        async def handler(msg):
            if msg.payload == b"boom":
                raise ValueError("boom")
            await self.app.send(b"ok")

        await self.start()
        with self.assertLogs("SALUS SDK", "ERROR"):
            self.fw.send(Frame(1, 1, "ws://c", b"boom"))
            self.fw.send(Frame(1, 2, "ws://c", b"fine"))
            self.assertEqual((await self.fw.read_frame()).payload, b"ok")
        await self.finish()

    async def test_inflight_handler_finishes_after_framework_closes(self):
        done = []

        @self.app.channel("c")
        async def handler(msg):
            await asyncio.sleep(0.2)
            done.append(msg.payload.as_str())

        await self.start()
        self.fw.send(Frame(1, 1, "ws://c", b"late"))
        await asyncio.sleep(0.05)
        await self.finish()
        self.assertEqual(done, ["late"])

    # -- queue mode and registry -------------------------------------------

    async def test_queue_channel_is_ordered_and_ends_on_close(self):
        channel = self.app.add_channel("stream")
        received = []

        async def main(app):
            async for msg in channel:
                received.append(msg.payload.as_str())

        await self.start(main)
        for i, word in enumerate(("a", "b", "c")):
            self.fw.send(Frame(1, i, "ws://stream", word.encode()))
        await asyncio.sleep(0.1)
        await self.finish()
        self.assertEqual(received, ["a", "b", "c"])

    async def test_handler_channel_cannot_be_iterated(self):
        channel = self.app.add_channel("h", lambda msg: None)
        with self.assertRaises(TypeError):
            channel.__aiter__()

    async def test_channel_send_uses_its_name(self):
        channel = self.app.add_channel("out")

        async def main(app):
            await channel.send(b"hi", to=2)

        await self.start(main)
        frame = await self.fw.read_frame()
        self.assertEqual((frame.frontend_id, frame.channel, frame.payload), (2, "ws://out", b"hi"))
        await self.finish()

    async def test_registry_rules(self):
        self.app.add_channel("x")
        with self.assertRaises(ValueError):
            self.app.add_channel("x")
        with self.assertRaises(ValueError):
            self.app.add_channel("ws://x")
        for bad in ("", "ws://", "http://x"):
            with self.assertRaises(ValueError):
                self.app.add_channel(bad)
        self.app.remove_channel("x")
        self.app.add_channel("x")

    async def test_close_ends_iteration_and_unclaims_channel(self):
        channel = self.app.add_channel("temp")
        ended = asyncio.Event()

        async def consume():
            async for _ in channel:
                pass
            ended.set()

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)
        channel.close()
        channel.close()
        await asyncio.wait_for(ended.wait(), 5)
        await consumer
        self.assertNotIn("temp", self.app._channels)

    async def test_channels_can_be_added_while_running(self):
        @self.app.channel("boot")
        async def boot(msg):
            @self.app.channel("late")
            async def late(m):
                await self.app.send(b"late reply")
            await self.app.send(b"registered")

        await self.start()
        self.fw.send(Frame(1, 1, "ws://boot", b""))
        self.assertEqual((await self.fw.read_frame()).payload, b"registered")
        self.fw.send(Frame(1, 2, "ws://late", b""))
        self.assertEqual((await self.fw.read_frame()).payload, b"late reply")
        await self.finish()

    # -- unclaimed messages ------------------------------------------------

    async def test_unclaimed_messages_reach_raw_iterator(self):
        self.app.add_channel("claimed", lambda msg: None)
        got = []

        async def main(app):
            async for msg in app.messages():
                got.append((msg.frontend_id, msg.channel, msg.payload.as_str()))

        await self.start(main)
        await asyncio.sleep(0.05)
        self.fw.send(Frame(1, 1, "ws://claimed", b"x"))
        self.fw.send(Frame(2, 2, "ws://other", b"y"))
        self.fw.send(Frame(3, 3, "custom://z", b"z"))
        await asyncio.sleep(0.1)
        await self.finish()
        self.assertEqual(got, [(2, "ws://other", "y"), (3, "custom://z", "z")])

    async def test_unclaimed_without_consumer_is_dropped_with_warning(self):
        await self.start()
        with self.assertLogs("SALUS SDK", "WARNING") as logs:
            self.fw.send(Frame(1, 1, "ws://nobody", b"x"))
            await asyncio.sleep(0.1)
        self.assertIn("ws://nobody", logs.output[0])
        await self.finish()


class MetaPayloadTest(unittest.TestCase):
    def test_golden_bytes(self):
        data = sdk.pack_meta({"id": 1}, b"\x00\xff")
        self.assertEqual(data, b"\x08\x00\x00\x00" + b'{"id":1}' + b"\x00\xff")
        self.assertEqual(sdk.unpack_meta(data), ({"id": 1}, b"\x00\xff"))

    def test_roundtrip_unicode_and_empty_body(self):
        meta = {"path": "/dök/ü", "headers": [["a", "b"]], "n": None}
        self.assertEqual(sdk.unpack_meta(sdk.pack_meta(meta)), (meta, b""))

    def test_unpack_errors(self):
        for bad in (b"", b"\x01\x00", b"\x05\x00\x00\x00{}", b"\x01\x00\x00\x00{",
                    b"\x02\x00\x00\x00[]", b"\x01\x00\x00\x00\xff"):
            with self.assertRaises(ProtocolError, msg=bad):
                sdk.unpack_meta(bad)

    def test_pack_rejects_unserializable(self):
        with self.assertRaises(sdk.PayloadError):
            sdk.pack_meta({"x": object()})


class SalusRequestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fw = FakeFramework()
        await self.fw.start()
        self.addAsyncCleanup(self.fw.stop)
        self.app = sdk.Plugin(self.fw.path)
        self.run = asyncio.create_task(self.app._run(None))
        self.addAsyncCleanup(self._cancel_run)
        await self.fw.accept()
        for _ in range(100):
            if self.app._connection is not None:
                break
            await asyncio.sleep(0.01)

    async def _cancel_run(self):
        if not self.run.done():
            self.run.cancel()
            await asyncio.gather(self.run, return_exceptions=True)

    async def read_request(self):
        frame = await self.fw.read_frame()
        meta, body = sdk.unpack_meta(frame.payload)
        return frame, meta, body

    def reply(self, topic, meta, body=b""):
        self.fw.send(Frame(0, 1, "salus://" + topic, sdk.pack_meta(meta, body)))

    async def test_request_success(self):
        task = asyncio.create_task(self.app.request("http/open", {"method": "GET", "path": "/x"}, b"body"))
        frame, meta, body = await self.read_request()
        self.assertEqual((frame.frontend_id, frame.channel), (sdk.NO_FRONTEND, "salus://http/open"))
        self.assertEqual((meta["method"], meta["path"], body), ("GET", "/x", b"body"))
        self.reply("http/open", {"id": meta["id"], "ok": True, "endpoint": "e1"}, b"extra")
        response = await asyncio.wait_for(task, 5)
        self.assertEqual(response.meta["endpoint"], "e1")
        self.assertEqual(response.body, b"extra")
        self.assertEqual(self.app._pending, {})

    async def test_request_rejected_raises_salus_error(self):
        task = asyncio.create_task(self.app.request("http/open", {"path": "/x"}))
        _, meta, _ = await self.read_request()
        self.reply("http/open", {"id": meta["id"], "ok": False, "error": "path already taken"})
        with self.assertRaisesRegex(sdk.SalusError, "path already taken"):
            await asyncio.wait_for(task, 5)

    async def test_request_without_error_text(self):
        task = asyncio.create_task(self.app.request("x"))
        _, meta, _ = await self.read_request()
        self.reply("x", {"id": meta["id"], "ok": False})
        with self.assertRaises(sdk.SalusError):
            await asyncio.wait_for(task, 5)

    async def test_request_timeout_and_late_reply(self):
        with self.assertRaises(sdk.SalusTimeout) as ctx:
            await self.app.request("slow", timeout=0.1)
        self.assertIsInstance(ctx.exception, TimeoutError)
        self.assertIsInstance(ctx.exception, sdk.SalusError)
        _, meta, _ = await self.read_request()
        self.assertEqual(self.app._pending, {})
        with self.assertLogs("SALUS SDK", "WARNING"):
            self.reply("slow", {"id": meta["id"], "ok": True})
            await asyncio.sleep(0.1)

    async def test_default_timeout_is_five_seconds(self):
        self.assertEqual(sdk.REQUEST_TIMEOUT, 5.0)

    async def test_concurrent_requests_answered_out_of_order(self):
        first = asyncio.create_task(self.app.request("t", {"n": 1}))
        second = asyncio.create_task(self.app.request("t", {"n": 2}))
        requests = {m["n"]: m["id"] for _, m, _ in [await self.read_request(), await self.read_request()]}
        self.assertEqual(len(set(requests.values())), 2)
        self.reply("t", {"id": requests[2], "ok": True, "answer": "two"})
        self.reply("t", {"id": requests[1], "ok": True, "answer": "one"})
        self.assertEqual((await asyncio.wait_for(first, 5)).meta["answer"], "one")
        self.assertEqual((await asyncio.wait_for(second, 5)).meta["answer"], "two")

    async def test_id_param_is_reserved(self):
        with self.assertRaises(ValueError):
            await self.app.request("t", {"id": 5})

    async def test_pending_request_fails_when_connection_closes(self):
        task = asyncio.create_task(self.app.request("t"))
        await self.read_request()
        self.fw.writer.close()
        with self.assertRaises(sdk.ConnectionClosed):
            await asyncio.wait_for(task, 5)

    async def test_cancelled_request_is_forgotten(self):
        task = asyncio.create_task(self.app.request("t"))
        await self.read_request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.app._pending, {})

    async def test_malformed_salus_frame_is_dropped(self):
        with self.assertLogs("SALUS SDK", "WARNING"):
            self.fw.send(Frame(0, 1, "salus://http/open", b"garbage"))
            await asyncio.sleep(0.1)
        self.assertFalse(self.run.done())

    async def test_unknown_salus_topic_without_id_is_unclaimed(self):
        got = []

        async def consume():
            async for msg in self.app.messages():
                got.append(msg.channel)

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        self.reply("future/feature", {"hello": 1})
        await asyncio.sleep(0.1)
        self.assertEqual(got, ["salus://future/feature"])
        consumer.cancel()

    async def test_frontend_lifecycle_events(self):
        attached, detached = [], []
        import threading

        @self.app.on_frontend_attached
        async def a1(fe):
            attached.append(("async", fe))

        @self.app.on_frontend_attached
        def a2(fe):
            attached.append(("sync", fe, threading.current_thread() is not threading.main_thread()))

        @self.app.on_frontend_detached
        async def d(fe):
            detached.append(fe)

        self.fw.send(Frame(11, 1, "salus://frontend/attached", sdk.pack_meta({})))
        self.fw.send(Frame(12, 2, "salus://frontend/attached", sdk.pack_meta({})))
        await asyncio.sleep(0.2)
        self.assertEqual(self.app.frontends, frozenset({11, 12}))
        self.assertEqual(sorted(a for a in attached if a[0] == "async"), [("async", 11), ("async", 12)])
        self.assertEqual(sorted(a[1:] for a in attached if a[0] == "sync"), [(11, True), (12, True)])

        self.fw.send(Frame(11, 3, "salus://frontend/detached", sdk.pack_meta({})))
        await asyncio.sleep(0.1)
        self.assertEqual(self.app.frontends, frozenset({12}))
        self.assertEqual(detached, [11])

    async def test_lifecycle_handler_exception_is_logged(self):
        @self.app.on_frontend_attached
        def broken(fe):
            raise RuntimeError("nope")

        with self.assertLogs("SALUS SDK", "ERROR"):
            self.fw.send(Frame(1, 1, "salus://frontend/attached", sdk.pack_meta({})))
            await asyncio.sleep(0.2)
        self.assertEqual(self.app.frontends, frozenset({1}))


class HttpTypesTest(unittest.TestCase):
    def test_response_helpers(self):
        r = sdk.Response.text("hi", 201, {"X-A": "1"})
        self.assertEqual((r.status, r.body), (201, "hi"))
        self.assertEqual(r.headers, [("content-type", "text/plain; charset=utf-8"), ("X-A", "1")])
        r = sdk.Response.json({"a": 1})
        self.assertEqual((r.status, r.body, r.headers), (200, '{"a":1}', [("content-type", "application/json")]))
        r = sdk.Response.bytes(b"\x00", content_type="image/png", headers=[("a", "b")])
        self.assertEqual(r.headers, [("content-type", "image/png"), ("a", "b")])

    def test_request_helpers(self):
        req = sdk.Request(1, "GET", "/p", "/p", {}, "a=1&a=2&b=&c=%C3%BC",
                          (("X-Thing", "v1"), ("x-thing", "v2")), sdk.Payload(b""), 3)
        self.assertEqual(req.query, {"a": "1", "b": "", "c": "ü"})
        self.assertEqual(req.query_all["a"], ["1", "2"])
        self.assertEqual(req.header("X-THING"), "v1")
        self.assertEqual(req.header("nope", "dflt"), "dflt")
        self.assertIsNone(req.header("nope"))

    def test_to_response_conversions(self):
        self.assertEqual(sdk._to_response(None).status, 204)
        self.assertEqual(sdk._to_response("x").body, "x")
        self.assertEqual(sdk._to_response(b"x").headers, [("content-type", "application/octet-stream")])
        self.assertEqual(sdk._to_response(sdk.Payload(b"x")).body, b"x")
        self.assertEqual(sdk._to_response([1]).body, "[1]")
        r = sdk.Response(404)
        self.assertIs(sdk._to_response(r), r)
        with self.assertRaises(TypeError):
            sdk._to_response(object())

    def test_http_error(self):
        self.assertEqual(sdk.HTTPError(404).message, "Not Found")
        self.assertEqual(sdk.HTTPError(404, "nope").message, "nope")
        self.assertEqual(sdk.HTTPError(299).message, "Error")
        for bad in (99, 600):
            with self.assertRaises(ValueError):
                sdk.HTTPError(bad)

    def test_encode_response_validates(self):
        self.assertEqual(sdk.unpack_meta(sdk._encode_response(4, sdk.Response.text("x")))[0]["status"], 200)
        for bad in (sdk.Response(99), sdk.Response(True), sdk.Response(200, body=5), sdk.Response("200")):
            with self.assertRaises((ValueError, TypeError)):
                sdk._encode_response(1, bad)


class HttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fw = FakeFramework()
        await self.fw.start()
        self.addAsyncCleanup(self.fw.stop)
        self.app = sdk.Plugin(self.fw.path)
        self.run = None
        self.next_mid = 100
        self.addAsyncCleanup(self._cancel_run)

    async def _cancel_run(self):
        if self.run is not None and not self.run.done():
            self.run.cancel()
            await asyncio.gather(self.run, return_exceptions=True)

    async def start(self, main=None, opens=0):
        self.run = asyncio.create_task(self.app._run(main))
        await self.fw.accept()
        return [await self.ack("http/open") for _ in range(opens)]

    async def connected(self):
        for _ in range(200):
            if self.app._connection is not None:
                return
            await asyncio.sleep(0.01)
        self.fail("plugin did not connect")

    async def expect_request(self, topic):
        frame = await self.fw.read_frame()
        self.assertEqual((frame.frontend_id, frame.channel), (sdk.NO_FRONTEND, "salus://" + topic))
        return sdk.unpack_meta(frame.payload)[0]

    def reply(self, topic, meta):
        self.fw.send(Frame(0, self._mid(), "salus://" + topic, sdk.pack_meta(meta)))

    async def ack(self, topic, ok=True, error=None):
        meta = await self.expect_request(topic)
        self.reply(topic, {"id": meta["id"], "ok": ok, **({"error": error} if error else {})})
        return meta

    def _mid(self):
        self.next_mid += 1
        return self.next_mid

    def http_request(self, route, *, fe=5, rid=7, method="GET", path=None, params=None,
                     query="", headers=None, body=b""):
        meta = {"id": rid, "method": method, "path": path or route, "route": route,
                "params": params or {}, "query_string": query, "headers": headers or []}
        self.fw.send(Frame(fe, self._mid(), "http://request", sdk.pack_meta(meta, body)))

    async def read_response(self):
        frame = await self.fw.read_frame()
        self.assertEqual(frame.channel, "http://response")
        meta, body = sdk.unpack_meta(frame.payload)
        return frame.frontend_id, meta, body

    # -- serving ----------------------------------------------------------

    async def test_static_route_opened_then_served_with_full_request(self):
        seen = []

        @self.app.route("get", "/hello/{name}")
        async def hello(req):
            seen.append(req)
            return {"hi": req.params["name"]}

        (opened,) = await self.start(opens=1)
        self.assertEqual((opened["method"], opened["path"]), ("GET", "/hello/{name}"))

        self.http_request("/hello/{name}", path="/hello/ann", params={"name": "ann"},
                          query="a=1&b=", headers=[["X-A", "1"]], body=b"payload", fe=5, rid=7)
        fe, meta, body = await self.read_response()
        self.assertEqual((fe, meta["id"], meta["status"]), (5, 7, 200))
        self.assertIn(["content-type", "application/json"], meta["headers"])
        self.assertEqual(json.loads(body), {"hi": "ann"})

        req = seen[0]
        self.assertEqual((req.frontend_id, req.request_id, req.method, req.path, req.route),
                         (5, 7, "GET", "/hello/ann", "/hello/{name}"))
        self.assertEqual((req.params, req.query, req.header("x-a"), req.body), ({"name": "ann"}, {"a": "1", "b": ""}, "1", b"payload"))
        await self.shutdown()

    async def shutdown(self):
        self.fw.writer.close()
        await asyncio.wait_for(self.run, 10)

    async def test_main_starts_after_routes_are_open(self):
        started = []

        @self.app.route("GET", "/x")
        async def x(req):
            return None

        async def main(app):
            started.append(True)

        await self.start(main)
        await asyncio.sleep(0.1)
        self.assertEqual(started, [])
        await self.ack("http/open")
        await asyncio.sleep(0.1)
        self.assertEqual(started, [True])
        await self.shutdown()

    async def test_return_value_conversions_on_the_wire(self):
        results = {
            "/text": "plain", "/bytes": b"\x00\x01", "/none": None, "/list": [1, 2],
            "/custom": sdk.Response(201, [("x-k", "v")], sdk.Payload(b"made")),
        }
        for path, result in results.items():
            self.app.route("GET", path)(lambda req, result=result: result)
        await self.start(opens=len(results))

        expected = {
            "/text": (200, b"plain", "text/plain; charset=utf-8"),
            "/bytes": (200, b"\x00\x01", "application/octet-stream"),
            "/none": (204, b"", None),
            "/list": (200, b"[1,2]", "application/json"),
            "/custom": (201, b"made", None),
        }
        for rid, (path, (status, body, ctype)) in enumerate(expected.items(), start=1):
            self.http_request(path, rid=rid)
            _, meta, got = await self.read_response()
            self.assertEqual((meta["id"], meta["status"], got), (rid, status, body), path)
            headers = dict(meta["headers"])
            self.assertEqual(headers.get("content-type"), ctype, path)
        self.assertIn(["x-k", "v"], meta["headers"])
        await self.shutdown()

    async def test_sync_handler_runs_in_worker_thread(self):
        import threading
        threads = []

        @self.app.route("POST", "/echo")
        def echo(req):
            threads.append(threading.current_thread())
            return req.body

        await self.start(opens=1)
        data = bytes(range(256)) * 12000
        self.http_request("/echo", method="POST", body=data)
        _, meta, body = await self.read_response()
        self.assertEqual((meta["status"], body), (200, data))
        self.assertIsNot(threads[0], threading.main_thread())
        await self.shutdown()

    async def test_method_case_is_normalized(self):
        @self.app.route("POST", "/p")
        async def p(req):
            return "ok"

        await self.start(opens=1)
        self.http_request("/p", method="post")
        self.assertEqual((await self.read_response())[1]["status"], 200)
        await self.shutdown()

    async def test_http_error(self):
        @self.app.route("GET", "/missing")
        async def missing(req):
            raise sdk.HTTPError(404, "nope", {"x-why": "gone"})

        @self.app.route("GET", "/teapot")
        async def teapot(req):
            raise sdk.HTTPError(418)

        await self.start(opens=2)
        self.http_request("/missing", rid=1)
        _, meta, body = await self.read_response()
        self.assertEqual((meta["status"], body), (404, b"nope"))
        self.assertIn(["x-why", "gone"], meta["headers"])
        self.http_request("/teapot", rid=2)
        _, meta, body = await self.read_response()
        self.assertEqual((meta["status"], body), (418, b"I'm a Teapot"))
        await self.shutdown()

    async def test_handler_failures_answer_500_and_plugin_continues(self):
        @self.app.route("GET", "/boom")
        async def boom(req):
            raise ValueError("secret detail")

        @self.app.route("GET", "/badtype")
        async def badtype(req):
            return object()

        @self.app.route("GET", "/badstatus")
        async def badstatus(req):
            return sdk.Response(99)

        @self.app.route("GET", "/badbody")
        async def badbody(req):
            return sdk.Response(200, body=12345)

        @self.app.route("GET", "/ok")
        async def ok(req):
            return "fine"

        await self.start(opens=5)
        with self.assertLogs("SALUS SDK", "ERROR"):
            for rid, path in enumerate(("/boom", "/badtype", "/badstatus", "/badbody"), start=1):
                self.http_request(path, rid=rid)
                _, meta, body = await self.read_response()
                self.assertEqual((meta["id"], meta["status"], body), (rid, 500, b"Internal Server Error"), path)
        self.http_request("/ok", rid=9)
        self.assertEqual((await self.read_response())[1]["status"], 200)
        await self.shutdown()

    async def test_unknown_route_and_bad_requests(self):
        await self.start()
        with self.assertLogs("SALUS SDK", "WARNING"):
            self.http_request("/nothing", rid=1)
            _, meta, body = await self.read_response()
            self.assertEqual((meta["id"], meta["status"], body), (1, 404, b"Not Found"))

            self.fw.send(Frame(5, 1, "http://request", sdk.pack_meta({"id": 2, "method": "GET"})))
            _, meta, _ = await self.read_response()
            self.assertEqual((meta["id"], meta["status"]), (2, 400))

            self.fw.send(Frame(5, 2, "http://request", sdk.pack_meta({"method": "GET"})))
            self.fw.send(Frame(5, 3, "http://request", b"garbage"))
            await asyncio.sleep(0.1)
        self.assertFalse(self.run.done())
        await self.shutdown()

    async def test_concurrent_requests_are_answered_independently(self):
        release = asyncio.Event()

        @self.app.route("GET", "/slow")
        async def slow(req):
            await release.wait()
            return "slow"

        @self.app.route("GET", "/fast")
        async def fast(req):
            release.set()
            return "fast"

        await self.start(opens=2)
        self.http_request("/slow", rid=1, fe=10)
        self.http_request("/fast", rid=2, fe=20)
        first = await self.read_response()
        second = await self.read_response()
        self.assertEqual([(r[0], r[1]["id"], r[2]) for r in (first, second)],
                         [(20, 2, b"fast"), (10, 1, b"slow")])
        await self.shutdown()

    # -- dynamic routes -----------------------------------------------------

    async def test_add_route_serve_and_close(self):
        await self.start()
        await self.connected()

        async def handler(req):
            return "dyn"

        task = asyncio.create_task(self.app.add_route("GET", "/dyn/{id}", handler))
        opened = await self.ack("http/open")
        self.assertEqual((opened["method"], opened["path"]), ("GET", "/dyn/{id}"))
        route = await asyncio.wait_for(task, 5)

        self.http_request("/dyn/{id}", rid=1)
        self.assertEqual((await self.read_response())[1]["status"], 200)

        close = asyncio.create_task(route.close())
        closed = await self.ack("http/close")
        self.assertEqual((closed["method"], closed["path"]), ("GET", "/dyn/{id}"))
        await asyncio.wait_for(close, 5)
        await asyncio.wait_for(route.close(), 5)

        with self.assertLogs("SALUS SDK", "WARNING"):
            self.http_request("/dyn/{id}", rid=2)
            self.assertEqual((await self.read_response())[1]["status"], 404)
        await self.shutdown()

    async def test_add_route_rejected_is_not_registered(self):
        await self.start()
        await self.connected()

        async def handler(req):
            return "x"

        task = asyncio.create_task(self.app.add_route("GET", "/taken", handler))
        await self.ack("http/open", ok=False, error="path already taken")
        with self.assertRaisesRegex(sdk.SalusError, "path already taken"):
            await asyncio.wait_for(task, 5)
        self.assertEqual(self.app._routes, {})

        task = asyncio.create_task(self.app.add_route("GET", "/taken", handler))
        await self.ack("http/open")
        await asyncio.wait_for(task, 5)
        await self.shutdown()

    async def test_close_rejected_keeps_route(self):
        await self.start()
        await self.connected()

        async def handler(req):
            return "still here"

        task = asyncio.create_task(self.app.add_route("GET", "/r", handler))
        await self.ack("http/open")
        route = await task
        close = asyncio.create_task(self.app.remove_route("GET", "/r"))
        await self.ack("http/close", ok=False, error="busy")
        with self.assertRaises(sdk.SalusError):
            await asyncio.wait_for(close, 5)
        self.http_request("/r")
        self.assertEqual((await self.read_response())[2], b"still here")
        self.assertIn(("GET", "/r"), self.app._routes)
        await self.shutdown()

    async def test_route_registration_rules(self):
        async def handler(req):
            return None

        self.app.route("GET", "/a")(handler)
        with self.assertRaises(ValueError):
            self.app.route("GET", "/a")(handler)
        with self.assertRaises(ValueError):
            self.app.route("FETCH", "/b")(handler)
        with self.assertRaises(ValueError):
            self.app.route("GET", "no-slash")(handler)
        self.app.route("POST", "/a")(handler)

        await self.start(opens=2)
        await self.connected()
        with self.assertRaises(RuntimeError):
            self.app.route("GET", "/late")(handler)
        await self.shutdown()

    async def test_static_route_rejected_fails_the_plugin(self):
        @self.app.route("GET", "/x")
        async def x(req):
            return None

        self.run = asyncio.create_task(self.app._run(None))
        await self.fw.accept()
        await self.ack("http/open", ok=False, error="not allowed")
        with self.assertRaisesRegex(sdk.SalusError, "not allowed"):
            await asyncio.wait_for(self.run, 5)


if __name__ == "__main__":
    unittest.main()
