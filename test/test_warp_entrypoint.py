"""Lifecycle tests, run ONLY in a disposable caomingjun/warp container.

Copy this file and docker/warp/entrypoint.sh into a separate /verification
directory. Run with --network none, --user root, --entrypoint python3 and
WARP_SUPERVISOR_TEST_SANDBOX=1. The test replaces binaries in the disposable
container layer; never set that marker in a normal or production container.
No project modules, configuration, registrations or production data are read.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


SANDBOX = (
    os.environ.get("WARP_SUPERVISOR_TEST_SANDBOX") == "1"
    and Path("/verification/entrypoint.sh").is_file()
    and hasattr(os, "geteuid")
    and os.geteuid() == 0
)

STUB = r'''#!/usr/bin/python3
import json, os, signal, sys, time
from pathlib import Path
cfg = json.loads(Path('/tmp/warp-supervisor-case.json').read_text())
root = Path(cfg['root'])
name = Path(sys.argv[0]).name
def event(text):
    with (root / 'events').open('a') as out:
        out.write(text + '\n')
event(name + ' ' + ' '.join(sys.argv[1:]))
if name == 'dbus-daemon':
    sys.exit(0)
if name == 'warp-cli':
    args = sys.argv[1:]
    if args[-2:] == ['registration', 'new']:
        Path('/var/lib/cloudflare-warp/reg.json').write_text('{}')
    if args[-1:] == ['connect']:
        (root / 'connected').touch()
    if args[-1:] == ['status']:
        if cfg.get('never_ready'):
            print('Status update: Connected (Limited)')
        elif cfg.get('drop_status') and (root / 'gost.pid').exists():
            if time.time() - (root / 'gost.pid').stat().st_mtime > 0.25:
                print('Status update: Disconnected')
                sys.exit(cfg.get('status_exit', 0))
            print('Status update: Connected')
        else:
            print('Status update: Connected' if (root / 'connected').exists()
                  else 'Status update: Disconnected')
    sys.exit(0)
(root / (name + '.pid')).write_text(str(os.getpid()))
event(name + ' uid=' + str(os.getuid()))
if name == 'gost' and not (root / 'connected').exists():
    event('ERROR proxy started before connected')
    sys.exit(90)
def terminate(*_):
    event(name + ' TERM')
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN if cfg.get('ignore_term') == name else terminate)
if cfg.get('exit_process') == name:
    time.sleep(cfg.get('exit_delay', 0.35))
    sys.exit(cfg.get('exit_code', 23))
while True:
    time.sleep(0.02)
'''


@unittest.skipUnless(SANDBOX, "requires an explicitly marked disposable WARP container")
class WarpEntrypointLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # These writes affect only this disposable container's overlay layer.
        for binary in ("warp-svc", "warp-cli", "gost", "dbus-daemon"):
            target = Path("/usr/bin") / binary
            target.write_text(STUB)
            target.chmod(0o755)
        Path("/dev/net").mkdir(parents=True, exist_ok=True)
        Path("/dev/net/tun").touch(exist_ok=True)
        Path("/var/lib/cloudflare-warp").mkdir(parents=True, exist_ok=True)
        Path("/var/lib/cloudflare-warp").chmod(0o777)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="warp-lifecycle-"))
        self.root.chmod(0o777)
        (self.root / "events").touch()
        (self.root / "events").chmod(0o666)
        self.process = None
        self.log = (self.root / "output.log").open("w+")

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=9)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.log.close()
        for name in ("warp-svc", "gost"):
            pid_file = self.root / (name + ".pid")
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def start(self, registered=True, **options):
        reg = Path("/var/lib/cloudflare-warp/reg.json")
        reg.unlink(missing_ok=True)
        if registered:
            reg.write_text("{}")
        Path("/tmp/warp-supervisor-case.json").write_text(json.dumps({
            "root": str(self.root), **options,
        }))
        env = {**os.environ, "WARP_SLEEP": "0.05", "GOST_ARGS": "-L=:1080",
               "WARP_ENABLE_NAT": "", "WARP_LICENSE_KEY": "",
               "REGISTER_WHEN_MDM_EXISTS": "", "DEBUG_ENABLE_QLOG": ""}

        def as_warp():
            os.setgid(1000)
            os.setuid(1000)

        self.process = subprocess.Popen(
            ["bash", "/verification/entrypoint.sh"], env=env,
            stdout=self.log, stderr=self.log, preexec_fn=as_warp,
        )

    def events(self):
        path = self.root / "events"
        return path.read_text() if path.exists() else ""

    def wait_started(self, name="gost"):
        deadline = time.monotonic() + 5
        while not (self.root / (name + ".pid")).exists():
            if time.monotonic() > deadline or self.process.poll() is not None:
                self.fail("process did not start: " + (self.root / "output.log").read_text())
            time.sleep(0.02)

    def assert_stopped(self):
        for name in ("warp-svc", "gost"):
            path = self.root / (name + ".pid")
            if path.exists():
                self.assertFalse(Path("/proc").joinpath(path.read_text()).exists(), name)
        self.assertNotIn("ERROR", self.events())

    def test_existing_registration_reconnects_and_term_stops_root_daemon(self):
        self.start()
        self.wait_started()
        self.assertIn("warp-cli --accept-tos connect", self.events())
        self.assertNotIn("registration new", self.events())
        self.assertIn("warp-svc uid=0", self.events())
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 143)
        self.assertIn("warp-svc TERM", self.events())
        self.assertIn("gost TERM", self.events())
        self.assert_stopped()

    def test_new_registration_connects(self):
        self.start(registered=False)
        self.wait_started()
        self.assertEqual(self.events().count("warp-cli registration new"), 1)
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 143)
        self.assert_stopped()

    def test_daemon_crash_stops_proxy(self):
        self.start(exit_process="warp-svc", exit_code=23)
        self.wait_started()
        self.assertEqual(self.process.wait(timeout=8), 23)
        self.assertIn("gost TERM", self.events())
        self.assert_stopped()

    def test_daemon_early_exit_never_starts_proxy(self):
        self.start(exit_process="warp-svc", exit_delay=0, exit_code=23)
        self.assertNotEqual(self.process.wait(timeout=8), 0)
        self.assertFalse((self.root / "gost.pid").exists())
        self.assert_stopped()

    def test_proxy_clean_exit_is_failure_and_stops_daemon(self):
        self.start(exit_process="gost", exit_code=0)
        self.wait_started()
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assertIn("warp-svc TERM", self.events())
        self.assert_stopped()

    def test_proxy_crash_stops_daemon(self):
        self.start(exit_process="gost", exit_code=24)
        self.wait_started()
        self.assertEqual(self.process.wait(timeout=8), 24)
        self.assert_stopped()

    def test_disconnected_tunnel_stops_live_daemon_and_proxy(self):
        self.start(drop_status=True)
        self.wait_started()
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assertIn("gost TERM", self.events())
        self.assert_stopped()

    def test_unreadable_tunnel_status_stops_proxy(self):
        self.start(drop_status=True, status_exit=2)
        self.wait_started()
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assert_stopped()

    def test_similar_connected_status_does_not_open_proxy(self):
        self.start(never_ready=True)
        self.wait_started("warp-svc")
        time.sleep(0.4)
        self.assertIsNone(self.process.poll())
        self.assertFalse((self.root / "gost.pid").exists())
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 143)
        self.assert_stopped()

    def test_unresponsive_root_daemon_has_bounded_shutdown(self):
        self.start(ignore_term="warp-svc")
        self.wait_started()
        started = time.monotonic()
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 143)
        self.assertLess(time.monotonic() - started, 8)
        self.assert_stopped()


if __name__ == "__main__":
    unittest.main(verbosity=2)
