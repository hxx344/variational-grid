"""Linux CI verifies explicit CLI imports hide input and reject non-terminals."""
import os
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(sys.platform == 'linux', 'Requires native Linux PTY')
class InstallerTerminalTests(unittest.TestCase):
    def test_explicit_cli_detached_terminal_hides_input_and_accepts_enter(self):
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = subprocess.Popen([sys.executable, '-c',
            'from variational_grid.cli import _read_session_token; value=_read_session_token(); print("accepted" if value == "fixture-secret" else "failed")'],
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO, 'getpass must disable echo before typing')
        os.write(master, b'fixture-secret\n')
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertIn(b'accepted', stdout)
        self.assertNotIn(b'fixture-secret', stdout + stderr)
        self.assertNotIn(b'Warning', stderr)

    def test_detached_pipe_refuses_to_read_or_echo_a_token(self):
        result = subprocess.run([sys.executable, '-c',
            'from variational_grid.cli import _read_session_token; from variational_grid.models import GridError\n'
            'try: _read_session_token()\n'
            'except GridError as error: print(str(error)); raise SystemExit(2)'],
            input=b'fixture-secret\n', capture_output=True, start_new_session=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'Hidden token input is unavailable', result.stdout)
        self.assertNotIn(b'fixture-secret', result.stdout + result.stderr)
        self.assertNotIn(b'Warning', result.stderr)
