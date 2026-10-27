"""One shell in a pseudo-terminal. No Salus dependency, so it can be tested on its own.

`PtySession` starts the program, reports its output and its end, takes input and a new size, and ends the shell (the whole
process group) when closed. Unix only: it uses `pty`. Everything runs on one asyncio event loop.
"""
import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
from typing import Callable

log = logging.getLogger("terminal")

READ_SIZE = 64 * 1024
# While this much output waits to be sent to the browser, the shell is not read from: a program that prints endlessly
# is slowed down by the pseudo-terminal instead of filling the memory.
MAX_PENDING_OUTPUT = 1024 * 1024


def set_size(fd: int, columns: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


class PtySession:
    def __init__(self, argv: list[str], cwd: str, columns: int, rows: int,
                 on_output: Callable[[bytes], None], on_exit: Callable[[int], None], env: dict | None = None):
        self._loop = asyncio.get_running_loop()
        self._on_output = on_output
        self._on_exit = on_exit
        self._pending_output = 0
        self._reading = False
        self._write_buffer = bytearray()
        self._writing = False
        self._closed = False

        self._master, slave = pty.openpty()
        set_size(self._master, columns, rows)
        os.set_blocking(self._master, False)
        try:
            self._process = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                # A session of its own whose controlling terminal is the pseudo-terminal: job control and Ctrl-C work.
                start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0), close_fds=True)
        except BaseException:
            os.close(self._master)
            raise
        finally:
            os.close(slave)
        self._start_reading()
        self._watcher = self._loop.create_task(self._wait_for_exit())

    @property
    def pid(self) -> int:
        return self._process.pid

    # ---- output
    def _start_reading(self) -> None:
        if not self._reading and not self._closed:
            self._loop.add_reader(self._master, self._read)
            self._reading = True

    def _stop_reading(self) -> None:
        if self._reading:
            self._loop.remove_reader(self._master)
            self._reading = False

    def _read(self) -> None:
        try:
            data = os.read(self._master, READ_SIZE)
        except BlockingIOError:
            return
        except OSError:  # EIO: the shell closed its end
            self._stop_reading()
            return
        if not data:
            self._stop_reading()
            return
        self._pending_output += len(data)
        if self._pending_output >= MAX_PENDING_OUTPUT:
            self._stop_reading()
        self._on_output(data)

    def output_sent(self, size: int) -> None:
        """The caller delivered `size` bytes of output; reading continues when enough room is free."""
        self._pending_output = max(0, self._pending_output - size)
        if self._pending_output < MAX_PENDING_OUTPUT // 2:
            self._start_reading()

    # ---- input
    def write(self, data: bytes) -> None:
        """Sends input to the shell in order, without blocking."""
        if self._closed:
            return
        self._write_buffer += data
        self._flush()

    def _flush(self) -> None:
        while self._write_buffer:
            try:
                written = os.write(self._master, self._write_buffer)
            except BlockingIOError:
                if not self._writing:
                    self._loop.add_writer(self._master, self._flush)
                    self._writing = True
                return
            except OSError:
                self._write_buffer.clear()
                break
            del self._write_buffer[:written]
        if self._writing:
            self._loop.remove_writer(self._master)
            self._writing = False

    def resize(self, columns: int, rows: int) -> None:
        if not self._closed:
            set_size(self._master, columns, rows)  # the shell gets SIGWINCH from the kernel

    # ---- end
    async def _wait_for_exit(self) -> None:
        code = await asyncio.to_thread(self._process.wait)
        if self._closed:
            return
        # What the shell printed last is still in the pseudo-terminal: deliver it before reporting the end.
        await asyncio.sleep(0.05)
        self._drain()
        self._release()
        self._on_exit(code)

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self._master, READ_SIZE)
            except OSError:
                return
            if not data:
                return
            self._on_output(data)

    def _release(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_reading()
        if self._writing:
            self._loop.remove_writer(self._master)
            self._writing = False
        os.close(self._master)

    def close(self) -> None:
        """Ends the shell and everything it started (hangup first, then kill) and frees the pseudo-terminal."""
        if self._closed and self._process.poll() is not None:
            return
        self._release()
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self._process.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            if sig == signal.SIGHUP:
                try:
                    self._process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        self._watcher.cancel()
