#!/usr/bin/env python3
"""Local integration tests: copied deploy scripts, real file moves, fake systemd.

Run: python3 services/jito-arb/deploy/tests/test_deploy.py
No root, build, network or system services required.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jito deploy ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "source"
        shutil.copytree(SOURCE / "deploy", self.src / "deploy")
        for name in ("config", "harness", "ui"):
            shutil.copytree(SOURCE / name, self.src / name)
        self.data = self.root / "var/lib/jito-arb"
        self.etc = self.root / "etc/jito-arb"
        self.opt = self.root / "opt/jito-arb"
        # Rewrite only absolute deployment roots in private script copies. Production
        # scripts deliberately expose no environment override for privileged paths.
        for name in ("install.sh", "reset-stats.sh"):
            p = self.src / "deploy" / name
            s = p.read_text()
            for old in ("/var/lib/jito-arb", "/etc/jito-arb", "/opt/jito-arb", "/etc/systemd/system"):
                s = s.replace(old, str(self.root) + old)
            # Unquoted literal install destinations need quoting after relocation.
            # Use a no-space symlink for those literals; $SRC still contains spaces.
            alias = Path(tempfile.mkdtemp(prefix="jito-deploy-path-")) / "root"
            alias.symlink_to(self.root, target_is_directory=True)
            self.addCleanup(shutil.rmtree, alias.parent)
            s = s.replace(str(self.root) + "/", str(alias) + "/")
            p.write_text(s)
        self.bin = self.root / "fakebin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        FAKE_STATE=str(self.root / "state.json"), FAKE_CALLS=str(self.root / "calls"))
        self.write_exe("id", '#!/bin/sh\nif [ "$1" = -u ]; then echo "${FAKE_UID:-0}"; else exit 0; fi\n')
        self.write_exe("chown", "#!/bin/sh\nexit 0\n")
        real_install = shutil.which("install")
        self.write_exe("install", f'''#!/usr/bin/env python3
import os, sys
args=[]
i=1
while i<len(sys.argv):
    if sys.argv[i] in ('-o','-g'): i+=2
    else: args.append(sys.argv[i]); i+=1
os.execv({real_install!r}, [{real_install!r}]+args)
''')
        self.write_exe("date", '#!/bin/sh\necho 20261106T010203Z\n')
        self.write_exe("systemctl", '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
p=Path(os.environ['FAKE_STATE'])
s=json.loads(p.read_text())
with open(os.environ['FAKE_CALLS'], 'a') as f: f.write(' '.join(args)+'\\n')
cmd=args[0]
units=[a for a in args[1:] if a.endswith('.service')]
if cmd=='is-active': sys.exit(0 if all(s.get(u)=='active' for u in units) else 3)
if cmd=='show':
    if os.environ.get('SHOW_FAIL'): sys.exit(1)
    print(s.get(units[0], 'inactive')); sys.exit(0)
if cmd=='stop':
    if os.environ.get('STOP_FAIL') in units: sys.exit(1)
    for u in units:
        if os.environ.get('STOP_STUCK') != u: s[u]='inactive'
if cmd=='start':
    if os.environ.get('START_FAIL'): sys.exit(1)
    for u in units: s[u]='active'
p.write_text(json.dumps(s))
''')
        self.set_state(False)
        self.protected = {}
        for rel, content in {
            "var/lib/jito-arb/pools.json": '{"pools":["operator-pool"]}',
            "var/lib/jito-arb/config/denylist.json": '{"mints":["operator-denied"]}',
            "var/lib/jito-arb/KILL": "stop trading\n",
            "etc/jito-arb/bot.toml": 'dry_run = true\n# operator settings\n',
            "etc/jito-arb/jito-arb.env": "HELIUS_API_KEY=local-test-sentinel\n",
            "opt/jito-arb/share/denylist.json": '{"mints":["shared-denied"]}',
            "var/lib/jito-arb/decisions/keep.txt": "operator note\n",
            "var/lib/jito-arb/frames/keep.jsonl": "not a diagnostic\n",
        }.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            self.protected[p] = p.read_bytes()
        self.logs = {}
        for rel in ("decisions/decisions.bin", "decisions/sim-details-20261106.jsonl",
                    "decisions/preproc-shadow.jsonl", "frames/frames.bin", "frames/preproc.bin"):
            p = self.data / rel
            p.write_bytes(("evidence: " + rel).encode())
            self.logs[rel] = p.read_bytes()

    def write_exe(self, name, content):
        p = self.bin / name
        p.write_text(content)
        p.chmod(0o755)

    def set_state(self, recorder):
        (self.root / "state.json").write_text(json.dumps({
            "jito-arb.service": "active", "jito-arb-ui.service": "active",
            "jito-arb-recorder.service": "active" if recorder else "inactive"}))

    def run_script(self, name, **env):
        return subprocess.run(["bash", str(self.src / "deploy" / name)],
                              env=dict(self.env, **env), text=True, capture_output=True)

    def check_protected(self):
        for p, content in self.protected.items():
            self.assertEqual(p.read_bytes(), content, str(p))

    def check_unmoved(self):
        for rel, content in self.logs.items():
            self.assertEqual((self.data / rel).read_bytes(), content)
        self.assertFalse((self.data / "archive").exists())
        self.check_protected()

    def test_root_required_before_any_side_effect(self):
        for script in ("reset-stats.sh", "install.sh"):
            result = self.run_script(script, FAKE_UID="1000")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("root", result.stderr)
        self.assertFalse((self.root / "calls").exists())
        self.check_unmoved()

    def test_each_writer_stop_failure_aborts_without_archive_or_restart(self):
        for unit in ("jito-arb.service", "jito-arb-ui.service", "jito-arb-recorder.service"):
            with self.subTest(unit=unit):
                self.set_state(True)
                result = self.run_script("reset-stats.sh", STOP_FAIL=unit)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("\nstart ", (self.root / "calls").read_text())
                self.check_unmoved()

    def test_stop_success_but_writer_still_active_aborts(self):
        result = self.run_script("reset-stats.sh", STOP_STUCK="jito-arb.service")
        self.assertNotEqual(result.returncode, 0)
        self.check_unmoved()

    def test_cannot_verify_stopped_aborts(self):
        self.assertNotEqual(self.run_script("reset-stats.sh", SHOW_FAIL="1").returncode, 0)
        self.check_unmoved()

    def test_archive_contents_unique_utc_and_recorder_restore(self):
        for recorder in (False, True):
            self.set_state(recorder)
            for rel, content in self.logs.items():
                (self.data / rel).write_bytes(content)
            result = self.run_script("reset-stats.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((self.root / "state.json").read_text())
            self.assertEqual(state["jito-arb-recorder.service"], "active" if recorder else "inactive")
            self.assertEqual(state["jito-arb.service"], "active")
            self.assertEqual(state["jito-arb-ui.service"], "active")
            self.check_protected()
        archives = list((self.data / "archive").iterdir())
        self.assertEqual(len(archives), 2)
        for archive in archives:
            self.assertRegex(archive.name, r"^20261106T010203Z\.[A-Za-z0-9]+$")
            for rel, content in self.logs.items():
                self.assertEqual((archive / rel).read_bytes(), content)
                self.assertFalse((self.data / rel).exists())
            self.assertEqual(len(list(archive.rglob("*.*"))), len(self.logs))

    def test_restart_failure_is_reported_and_evidence_retained(self):
        result = self.run_script("reset-stats.sh", START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        archive, = (self.data / "archive").iterdir()
        for rel, content in self.logs.items():
            self.assertEqual((archive / rel).read_bytes(), content)
        self.check_protected()

    def test_install_preserves_operator_files_and_installs_probe_and_seed(self):
        build = self.src / "build/vm"
        build.mkdir(parents=True)
        for name in ("jito-arb", "quote-probe"):
            p = build / name
            p.write_text("#!/bin/sh\necho test " + name + "\n")
            p.chmod(0o755)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        for _ in range(2):
            result = self.run_script("install.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.check_protected()
        self.assertEqual((self.opt / "bin/quote-probe").read_bytes(), (build / "quote-probe").read_bytes())
        self.assertTrue(os.access(self.opt / "bin/quote-probe", os.X_OK))
        self.assertEqual((self.opt / "share/holders_seed.json").read_bytes(),
                         (self.src / "harness/holders_seed.json").read_bytes())
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(calls, ["daemon-reload", "daemon-reload"])
        # A fresh denylist is still provisioned when no operator copy exists.
        for p in (self.opt / "share/denylist.json", self.data / "config/denylist.json"):
            p.unlink()
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        for p in (self.opt / "share/denylist.json", self.data / "config/denylist.json"):
            self.assertEqual(p.read_bytes(), (self.src / "config/denylist.json").read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
