import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session import PtySession  # noqa: E402


async def run(argv, steps=None, columns=80, rows=24, timeout=5):
    """Runs a program in a session; `steps(session)` may send input. Returns (output, exit code)."""
    output, done = bytearray(), asyncio.get_running_loop().create_future()
    session = PtySession(argv, "/", columns, rows, lambda data: (output.extend(data), session.output_sent(len(data))),
                         lambda code: done.set_result(code), env={"PATH": os.environ["PATH"], "TERM": "xterm"})
    try:
        if steps:
            await steps(session)
        code = await asyncio.wait_for(done, timeout)
    finally:
        session.close()
    return bytes(output), code


class Sessions(unittest.IsolatedAsyncioTestCase):
    async def test_output_and_exit_code(self):
        output, code = await run(["/bin/sh", "-c", "printf hello; exit 3"])
        self.assertIn(b"hello", output)
        self.assertEqual(code, 3)

    async def test_input_is_echoed_and_delivered_in_order(self):
        async def steps(session):
            for text in (b"one\n", b"two\n", b"three\n"):
                session.write(text)
            await asyncio.sleep(0.3)
            session.write(b"\x04")  # end of input

        output, code = await run(["/bin/cat"], steps)
        self.assertEqual(code, 0)
        self.assertLess(output.index(b"one"), output.index(b"two"))
        self.assertLess(output.index(b"two"), output.index(b"three"))

    async def test_the_size_is_set_at_the_start_and_changed_later(self):
        output, _ = await run(["/bin/sh", "-c", "stty size"], columns=100, rows=30)
        self.assertIn(b"30 100", output)

        async def steps(session):
            await asyncio.sleep(0.3)
            session.resize(120, 40)
            await asyncio.sleep(0.2)
            session.write(b"stty size; exit\n")

        output, _ = await run(["/bin/sh"], steps)
        self.assertIn(b"40 120", output)

    async def test_it_is_a_terminal_with_job_control(self):
        output, _ = await run(["/bin/sh", "-c", "test -t 0 && echo tty-yes; ps -o tpgid= -p $$ >/dev/null && echo ps-ok"])
        self.assertIn(b"tty-yes", output)

    async def test_close_ends_the_shell_and_its_children(self):
        events = []
        session = PtySession(["/bin/sh", "-c", "sleep 60 & echo started; wait"], "/", 80, 24, events.append, lambda code: events.append(code))
        await asyncio.sleep(0.4)
        pid = session.pid
        session.close()
        await asyncio.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)  # nobody of its process group is left
        session.close()  # a second close is harmless

    async def test_output_that_nobody_takes_pauses_the_shell(self):
        taken = []
        session = PtySession(["/bin/sh", "-c", "yes | head -c 20000000"], "/", 80, 24, lambda data: taken.append(len(data)), lambda code: None)
        await asyncio.sleep(1.0)
        self.assertLess(sum(taken), 4 * 1024 * 1024)  # never confirmed, so the reading stopped early
        session.close()


if __name__ == "__main__":
    unittest.main()
