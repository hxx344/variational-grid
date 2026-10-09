"""CI archive fixtures reuse the real installer and isolated service filesystem."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_installer_fixture", ROOT / "tests/test_install.py")
source_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_tests)
spec = importlib.util.spec_from_file_location("ci_package", ROOT / "deploy/package-release.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


@unittest.skipUnless(sys.platform == "linux", "Linux installer integration")
class CiInstallerTests(unittest.TestCase):
    git = source_tests.InstallTests.git
    commit = source_tests.InstallTests.commit
    install = source_tests.InstallTests.install
    calls = source_tests.InstallTests.calls
    restarts = source_tests.InstallTests.restarts

    def setUp(self):
        source_tests.InstallTests.setUp(self)
        curl = self.bin / "curl"
        curl.write_text(f'''#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
url = next(value for value in args if value.startswith('https://'))
if not url.startswith('https://github.com/hxx344/variational-grid/releases/'):
    raise SystemExit('Unexpected installer network request')
target = args[args.index('-o') + 1]
name = url.rsplit('/', 1)[1]
with open(os.environ['GRID_INSTALL_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps(['ci-download', name]) + '\\n')
shutil.copyfile(Path(os.environ['GRID_CI_OUTPUT']) / name, target)
''')
        curl.chmod(0o755)
        self.env["GRID_CI_OUTPUT"] = str(self.root / "ci-release")
        service = self.bin / "systemctl"
        script = service.read_text()
        script = script.replace('    if args[0] == "restart":', '''    if args[0] == "restart" and os.environ.get("GRID_FAIL_CI_START"):
        once = Path(os.environ["GRID_FAIL_CI_START"])
        if not once.exists():
            once.touch()
            raise SystemExit(1)
    if args[0] == "restart":''')
        service.write_text(script)

    def release(self):
        return package.build_release(self.source, self.git("rev-parse", "HEAD"), Path(self.env["GRID_CI_OUTPUT"]))

    def test_source_to_ci_noop_and_web_only_update_keep_engine_running(self):
        self.install()
        self.release()
        self.env.pop("PROJECT_DEPLOY_MODE", None)
        self.log.write_text("")
        self.install()
        current = (self.app / "current").resolve()
        self.assertTrue(current.name.endswith("-ci"))
        self.assertEqual(self.restarts(), [], "Identical engine/web trees must retain both running services")
        self.assertFalse(any(call[0] == "git" for call in self.calls()))
        self.log.write_text("")
        self.install()
        self.assertEqual((self.app / "current").resolve(), current)
        self.assertEqual(self.restarts(), [])
        self.assertEqual([call for call in self.calls() if call[0] == "ci-download"], [["ci-download", "release-manifest.json"]])
        (self.source / "README.md").write_text("documentation update")
        self.commit()
        self.release()
        self.log.write_text("")
        self.install()
        self.assertEqual((self.app / "current").resolve(), current)
        self.assertEqual(self.restarts(), [])
        self.assertEqual([call for call in self.calls() if call[0] == "ci-download"], [["ci-download", "release-manifest.json"]])
        with (self.source / "variational_grid/web/qqq.js").open("a") as stream:
            stream.write("\n// CI web update\n")
        self.commit()
        self.release()
        self.log.write_text("")
        self.install()
        self.assertEqual(self.restarts(), ["variational-grid-web.service"])

    def test_bad_archive_and_failed_service_update_restore_source_release(self):
        self.install()
        current = (self.app / "current").resolve()
        configuration = {path.name: path.read_bytes() for path in self.conf.iterdir() if path.is_file()}
        with (self.source / "variational_grid/web/qqq.js").open("a") as stream:
            stream.write("\n// changed web runtime\n")
        self.commit()
        manifest = self.release()
        archive = Path(self.env["GRID_CI_OUTPUT"]) / manifest["artifacts"]["linux-x64"]["file"]
        valid = archive.read_bytes()
        archive.write_bytes(b"damaged archive")
        self.env.pop("PROJECT_DEPLOY_MODE", None)
        self.install(expected=1)
        self.assertEqual((self.app / "current").resolve(), current)
        archive.write_bytes(valid)
        self.env["GRID_FAIL_CI_START"] = str(self.root / "failed-start-once")
        self.log.write_text("")
        self.install(expected=1)
        self.assertEqual((self.app / "current").resolve(), current)
        self.assertEqual({path.name: path.read_bytes() for path in self.conf.iterdir() if path.is_file()}, configuration)
        self.assertEqual(self.restarts(), ["variational-grid-web.service", "variational-grid-web.service"])
        self.assertTrue(json.loads(self.service_state.read_text())["variational-grid-web.service"]["active"])
        self.assertFalse(list(self.app.glob(".ci-release.*")))


if __name__ == "__main__":
    unittest.main()
