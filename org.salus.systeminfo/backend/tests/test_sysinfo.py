import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sysinfo  # noqa: E402

GIB = 1024 ** 3

NVIDIA_SMI = """0, NVIDIA GeForce RTX 4090, 24564, 1024, 23540, 550.54.14
1, NVIDIA A100-SXM4-80GB, 81920, [N/A], [N/A], 550.54.14
"""

SYSTEM_PROFILER_M4 = json.dumps({"SPDisplaysDataType": [{
    "_name": "Apple M4 Pro", "sppci_model": "Apple M4 Pro", "sppci_cores": "20",
    "spdisplays_vendor": "sppci_vendor_Apple", "spdisplays_mtlgpufamilysupport": "spdisplays_metal3",
}]})

SYSTEM_PROFILER_INTEL = json.dumps({"SPDisplaysDataType": [
    {"sppci_model": "Intel UHD Graphics 630", "spdisplays_vram_shared": "1536 MB", "spdisplays_vendor": "Intel"},
    {"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vram": "8 GB", "spdisplays_vendor": "sppci_vendor_amd"},
]})


def runner(outputs):
    """A stand-in for run_command: the output per command name, None (not installed) for the others."""
    calls = []

    def run(args):
        calls.append(args)
        return outputs.get(args[0])
    run.calls = calls
    return run


no_torch = lambda: False  # noqa: E731


class ParseTest(unittest.TestCase):
    def test_nvidia_smi(self):
        gpus = sysinfo.parse_nvidia_smi(NVIDIA_SMI)
        self.assertEqual([g["name"] for g in gpus], ["NVIDIA GeForce RTX 4090", "NVIDIA A100-SXM4-80GB"])
        self.assertEqual(gpus[0]["memory_total"], 24564 * 1024 * 1024)
        self.assertEqual(gpus[0]["memory_free"], 23540 * 1024 * 1024)
        self.assertEqual(gpus[0]["index"], 0)
        self.assertIsNone(gpus[1]["memory_used"], "values nvidia-smi does not know stay unknown")
        self.assertEqual(sysinfo.parse_nvidia_smi(""), [])
        self.assertEqual(sysinfo.parse_nvidia_smi("garbage"), [])

    def test_system_profiler(self):
        [m4] = sysinfo.parse_system_profiler(SYSTEM_PROFILER_M4)
        self.assertEqual((m4["name"], m4["cores"], m4["vendor"], m4["metal"]), ("Apple M4 Pro", 20, "Apple", "Metal 3"))
        intel, amd = sysinfo.parse_system_profiler(SYSTEM_PROFILER_INTEL)
        self.assertEqual(intel["vram"], 1536 * 1024 * 1024)
        self.assertEqual(amd["vram"], 8 * GIB)
        self.assertEqual(sysinfo.parse_system_profiler("not json"), [])
        self.assertEqual(sysinfo.parse_system_profiler("{}"), [])

    def test_vram_and_versions(self):
        self.assertEqual(sysinfo.parse_vram("2 TB"), 2 * 1024 * GIB)
        for bad in (None, "", "8GB", "x GB", "8 PB"):
            self.assertIsNone(sysinfo.parse_vram(bad), bad)
        self.assertEqual(sysinfo.macos_version_tuple("15.1.2"), (15, 1, 2))
        self.assertEqual(sysinfo.macos_version_tuple(""), ())

    def test_apple_core_kinds(self):
        def run(args):
            return {"hw.perflevel0.physicalcpu": "10\n", "hw.perflevel1.physicalcpu": "4\n"}.get(args[-1])
        self.assertEqual(sysinfo.apple_core_kinds(run), {"performance_cores": 10, "efficiency_cores": 4})
        self.assertEqual(sysinfo.apple_core_kinds(runner({})), {})

    def test_proc_cpuinfo(self):
        self.assertEqual(sysinfo.parse_proc_cpuinfo("processor\t: 0\nmodel name\t: AMD EPYC 7742 64-Core Processor\n"),
                         "AMD EPYC 7742 64-Core Processor")
        self.assertEqual(sysinfo.parse_proc_cpuinfo("Hardware\t: BCM2835\nModel\t: Raspberry Pi 4\n"), "Raspberry Pi 4")
        self.assertIsNone(sysinfo.parse_proc_cpuinfo(""))


class GpuTest(unittest.TestCase):
    def test_nvidia_from_nvidia_smi(self):
        run = runner({"nvidia-smi": NVIDIA_SMI})
        gpus = sysinfo.gpus(run, system="Linux", machine="x86_64", torch_present=no_torch)
        self.assertEqual(len(gpus), 2)
        self.assertTrue(all(g["vendor"] == "NVIDIA" and g["source"] == "nvidia-smi" for g in gpus))

    def test_no_gpu(self):
        self.assertEqual(sysinfo.gpus(runner({}), system="Linux", machine="x86_64", torch_present=no_torch), [])
        self.assertEqual(sysinfo.gpus(runner({}), system="Windows", machine="AMD64", torch_present=no_torch), [])

    def test_torch_is_asked_only_without_nvidia_smi(self):
        asked = []
        sysinfo.gpus(runner({"nvidia-smi": NVIDIA_SMI}), system="Linux", machine="x86_64", torch_present=lambda: asked.append(1))
        self.assertEqual(asked, [])

    def test_apple_silicon(self):
        run = runner({"sysctl": "Apple M4 Pro\n", "system_profiler": SYSTEM_PROFILER_M4})
        [gpu] = sysinfo.apple_gpus(run, total_memory=48 * GIB, torch_present=no_torch, mac_version="15.1")
        self.assertEqual((gpu["vendor"], gpu["name"], gpu["chip"], gpu["cores"]), ("Apple", "Apple M4 Pro", "Apple M4 Pro", 20))
        self.assertTrue(gpu["unified_memory"])
        self.assertEqual(gpu["memory_total"], 48 * GIB)
        self.assertTrue(gpu["mps"]["available"])
        self.assertIn("PyTorch is not installed", gpu["mps"]["source"])
        self.assertNotIn(["nvidia-smi"], [c[:1] for c in run.calls], "no NVIDIA tools on a Mac with Apple Silicon")

    def test_apple_silicon_without_system_profiler_and_old_macos(self):
        [gpu] = sysinfo.apple_gpus(runner({"sysctl": "Apple M1\n"}), total_memory=8 * GIB, torch_present=no_torch, mac_version="11.6")
        self.assertEqual(gpu["name"], "Apple M1")
        self.assertIsNone(gpu["cores"])
        self.assertFalse(gpu["mps"]["available"])

    def test_intel_mac(self):
        gpus = sysinfo.gpus(runner({"system_profiler": SYSTEM_PROFILER_INTEL}), system="Darwin", machine="x86_64", torch_present=no_torch)
        self.assertEqual([g["name"] for g in gpus], ["Intel UHD Graphics 630", "AMD Radeon Pro 5500M"])
        self.assertEqual(gpus[1]["memory_total"], 8 * GIB)

    def test_run_command_of_a_missing_tool(self):
        self.assertIsNone(sysinfo.run_command(["salus-no-such-tool-xyz"]))
        self.assertIsNone(sysinfo.run_command([sys.executable, "-c", "import sys; sys.exit(3)"]))
        self.assertEqual(sysinfo.run_command([sys.executable, "-c", "print('hi')"]).strip(), "hi")


class CollectTest(unittest.TestCase):
    def test_this_machine(self):
        info = sysinfo.collect()
        json.dumps(info)  # what the route sends
        mem = info["memory"]
        self.assertGreater(mem["total"], 0)
        self.assertTrue(0 < mem["available"] <= mem["total"])
        cpu = info["cpu"]
        self.assertGreaterEqual(cpu["logical_cores"], 1)
        if cpu["physical_cores"] is not None:
            self.assertLessEqual(cpu["physical_cores"], cpu["logical_cores"])
        self.assertIsInstance(info["gpus"], list)
        if sysinfo.is_apple_silicon():
            self.assertEqual(info["gpus"][0]["memory_total"], mem["total"])
            self.assertTrue(info["cpu"]["model"].startswith("Apple"))


class RouteTest(unittest.TestCase):
    def test_info_route(self):
        import plugin
        response = plugin.info(None)
        self.assertEqual(response.status, 200)
        self.assertIn(("cache-control", "no-store"), list(response.headers))


if __name__ == "__main__":
    unittest.main()
