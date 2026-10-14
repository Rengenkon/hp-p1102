"""Exercise reconciliation against drift and repeated runs, without root or USB."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from runtime import reconcile_queue


class Queue:
    def __init__(self, directory):
        self.directory = directory
        self.exists = False
        self.uri = ""
        self.options = {}
        self.mutations = []
        self.default = "other_printer"
        self.disabled = False
        self.accepting = True

    def run(self, args, check=True):
        output, code = "", 0
        if args[:2] == ["lpstat", "-v"]:
            output = f"device for printer: {self.uri}\n"
            code = 0 if self.exists else 1
        elif args[0] == "lpoptions":
            if "-l" in args:
                output = "\n".join(f"{k}/{k}: *{v}" for k, v in self.options.items())
            else:
                output = " ".join(f"{k}={v}" for k, v in self.options.items() if k != "PageSize")
        elif args[:2] == ["lpstat", "-p"]:
            output = "printer printer " + ("disabled" if self.disabled else "is idle")
        elif args[:2] == ["lpstat", "-a"]:
            output = "printer " + ("accepting requests" if self.accepting else "not accepting requests")
        elif args[:2] == ["lpstat", "-d"]:
            output = "system default destination: " + self.default
        elif args[:2] == ["lpadmin", "-p"]:
            self.mutations.append(args)
            self.exists = True
            if "-v" in args:
                self.uri = args[args.index("-v") + 1]
            if "-m" in args:
                (self.directory / "printer.ppd").write_text("driver=" + args[args.index("-m") + 1])
            for index, arg in enumerate(args):
                if arg == "-o":
                    key, value = args[index + 1].split("=", 1)
                    self.options[key] = value
                elif arg == "-R":
                    self.options.pop(args[index + 1], None)
        elif args[:2] == ["lpadmin", "-d"]:
            self.mutations.append(args)
            self.default = args[2]
        elif args[0] == "cupsenable":
            self.mutations.append(args)
            self.disabled = False
        elif args[0] == "cupsaccept":
            self.mutations.append(args)
            self.accepting = True
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, output, "")


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.queue = Queue(self.path)

    def apply(self, options=None, **kwargs):
        reconcile_queue(self.queue.run, "printer", "ipp://localhost/printer", "everywhere",
                        options or {"PageSize": "A4"}, self.path / "state.json", self.path,
                        emit=lambda message: None, **kwargs)

    def test_repeat_performs_no_mutation_and_preserves_state_file(self):
        self.apply()
        self.queue.mutations.clear()
        before = (self.path / "state.json").stat().st_mtime_ns
        self.apply()
        self.assertEqual(self.queue.mutations, [])
        self.assertEqual((self.path / "state.json").stat().st_mtime_ns, before)

    def test_uri_and_option_drift_repaired_without_reinstalling_driver(self):
        self.apply()
        self.queue.uri = "ipp://wrong/printer"
        self.queue.options["PageSize"] = "Letter"
        self.queue.mutations.clear()
        self.apply()
        self.assertEqual(self.queue.uri, "ipp://localhost/printer")
        self.assertEqual(self.queue.options["PageSize"], "A4")
        self.assertNotIn("-m", self.queue.mutations[0])

    def test_ppd_drift_repairs_driver(self):
        self.apply()
        (self.path / "printer.ppd").write_text("unexpected driver")
        self.queue.mutations.clear()
        self.apply()
        self.assertIn("-m", self.queue.mutations[0])

    def test_removed_managed_option_leaves_unmanaged_option(self):
        self.apply({"PageSize": "A4", "Duplex": "None"})
        self.queue.options["unmanaged"] = "keep"
        self.apply({"PageSize": "A4"})
        self.assertNotIn("Duplex", self.queue.options)
        self.assertEqual(self.queue.options["unmanaged"], "keep")

    def test_repair_paused_queue_and_preserve_other_default(self):
        self.apply(make_default=False)
        self.queue.disabled = True
        self.queue.accepting = False
        self.apply(make_default=False)
        self.assertFalse(self.queue.disabled)
        self.assertTrue(self.queue.accepting)
        self.assertEqual(self.queue.default, "other_printer")


if __name__ == "__main__":
    unittest.main()
