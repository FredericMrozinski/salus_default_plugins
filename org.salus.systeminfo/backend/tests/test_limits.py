import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import limits  # noqa: E402
import sysinfo  # noqa: E402

GIB = 1024 ** 3
V1_UNLIMITED = "9223372036854771712"


class FakeRoot(unittest.TestCase):
    """A folder that looks like / of a Linux machine or container."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def v2(self, own="/"):
        self.write("sys/fs/cgroup/cgroup.controllers", "cpuset cpu io memory pids")
        self.write("proc/self/cgroup", f"0::{own}\n")


class CgroupV2Test(FakeRoot):
    def test_docker_container_with_its_own_cgroup_namespace(self):
        self.v2()
        self.write("sys/fs/cgroup/memory.max", str(4 * GIB))
        self.write("sys/fs/cgroup/memory.current", str(3 * GIB))
        self.write("sys/fs/cgroup/memory.stat", f"anon 1\ninactive_file {GIB}\nactive_file 5\n")
        self.write("sys/fs/cgroup/cpu.max", "150000 100000")
        self.write("sys/fs/cgroup/cpuset.cpus.effective", "0-3")
        self.assertEqual(limits.memory_limit(self.root),
                         {"limit": 4 * GIB, "available": 2 * GIB, "usage": 2 * GIB, "source": "cgroup v2", "kind": "cgroup"})
        self.assertEqual(limits.cpu_quota(self.root), {"cores": 1.5, "source": "cgroup v2"})
        self.assertEqual(limits.cpuset_count(self.root), 4)
        self.assertEqual(limits.effective_cpus(16, self.root)["cores"], 1.5)
        self.assertEqual(limits.effective_cpus(16, self.root)["limited_by"], "quota")

    def test_the_tightest_ancestor_counts(self):
        # Kubernetes: the pod caps memory, the container below it does not; the CPU cap is on the container.
        own = "/kubepods/pod1/ctr"
        self.v2(own)
        self.write("sys/fs/cgroup/kubepods/pod1/memory.max", str(2 * GIB))
        self.write("sys/fs/cgroup/kubepods/pod1/memory.current", str(GIB // 2))
        self.write("sys/fs/cgroup/kubepods/pod1/ctr/memory.max", "max")
        self.write("sys/fs/cgroup/kubepods/pod1/ctr/memory.current", str(GIB // 4))
        self.write("sys/fs/cgroup/kubepods/pod1/ctr/cpu.max", "50000 100000")
        self.write("sys/fs/cgroup/kubepods/cpu.max", "max 100000")
        cap = limits.memory_limit(self.root)
        self.assertEqual((cap["limit"], cap["available"]), (2 * GIB, 2 * GIB - GIB // 2))
        self.assertEqual(limits.cpu_quota(self.root)["cores"], 0.5)

    def test_no_caps(self):
        self.v2("/user.slice/session-1.scope")
        self.write("sys/fs/cgroup/user.slice/memory.max", "max")
        self.write("sys/fs/cgroup/user.slice/cpu.max", "max 100000")
        self.assertIsNone(limits.memory_limit(self.root))
        self.assertIsNone(limits.cpu_quota(self.root))
        self.assertEqual(limits.effective_cpus(8, self.root), {"cores": 8.0, "limited_by": None, "quota": None, "cpuset": None, "spawner": None})

    def test_a_cpuset_smaller_than_the_machine(self):
        self.v2()
        self.write("sys/fs/cgroup/cpuset.cpus.effective", "0-1,6")
        effective = limits.effective_cpus(12, self.root)
        self.assertEqual((effective["cores"], effective["limited_by"]), (3.0, "cpuset"))


class CgroupV1Test(FakeRoot):
    def test_container_sees_its_cgroup_at_the_mount_point(self):
        self.write("proc/self/cgroup", "5:memory:/docker/abc\n4:cpu,cpuacct:/docker/abc\n3:cpuset:/docker/abc\n")
        self.write("sys/fs/cgroup/memory/memory.limit_in_bytes", str(GIB))
        self.write("sys/fs/cgroup/memory/memory.usage_in_bytes", str(GIB // 2))
        self.write("sys/fs/cgroup/memory/memory.stat", f"cache 1\ntotal_inactive_file {GIB // 4}\n")
        self.write("sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us", "200000")
        self.write("sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us", "100000")
        self.write("sys/fs/cgroup/cpuset/cpuset.cpus", "0-7")
        self.assertEqual(limits.cgroup_version(self.root), 1)
        cap = limits.memory_limit(self.root)
        self.assertEqual((cap["limit"], cap["usage"], cap["available"], cap["source"]), (GIB, GIB // 4, 3 * GIB // 4, "cgroup v1"))
        self.assertEqual(limits.cpu_quota(self.root)["cores"], 2.0)
        self.assertEqual(limits.cpuset_count(self.root), 8)

    def test_unlimited_is_no_cap(self):
        self.write("proc/self/cgroup", "5:memory:/\n4:cpu:/\n")
        self.write("sys/fs/cgroup/memory/memory.limit_in_bytes", V1_UNLIMITED)
        self.write("sys/fs/cgroup/cpu/cpu.cfs_quota_us", "-1")
        self.write("sys/fs/cgroup/cpu/cpu.cfs_period_us", "100000")
        self.assertIsNone(limits.memory_limit(self.root))
        self.assertIsNone(limits.cpu_quota(self.root))

    def test_no_cgroups_at_all(self):
        self.assertIsNone(limits.cgroup_version(self.root))
        self.assertIsNone(limits.memory_limit(self.root))
        self.assertIsNone(limits.cpu_quota(self.root))


class MemoryTest(FakeRoot):
    def fake_vm(self, total, available):
        return mock.patch.object(sysinfo.psutil, "virtual_memory", return_value=mock.Mock(total=total, available=available, percent=50.0))

    def test_a_container_cap_is_the_effective_memory(self):
        self.v2()
        self.write("sys/fs/cgroup/memory.max", str(4 * GIB))
        self.write("sys/fs/cgroup/memory.current", str(GIB))
        with self.fake_vm(64 * GIB, 40 * GIB):
            mem = sysinfo.memory(self.root, environ={})
        self.assertEqual((mem["total"], mem["available"]), (4 * GIB, 3 * GIB))
        self.assertEqual((mem["machine_total"], mem["machine_available"]), (64 * GIB, 40 * GIB))
        self.assertEqual(mem["used_percent"], 25.0)
        self.assertEqual(mem["limit"]["source"], "cgroup v2")

    def test_the_machine_can_be_the_tighter_one(self):
        self.v2()
        self.write("sys/fs/cgroup/memory.max", str(4 * GIB))
        self.write("sys/fs/cgroup/memory.current", "0")
        with self.fake_vm(64 * GIB, GIB):  # the host itself is almost full
            self.assertEqual(sysinfo.memory(self.root, environ={})["available"], GIB)

    def test_without_a_cap_the_machine_counts(self):
        with self.fake_vm(16 * GIB, 8 * GIB):
            mem = sysinfo.memory(self.root, environ={})
        self.assertEqual((mem["total"], mem["available"], mem["limit"]), (16 * GIB, 8 * GIB, None))


class SpawnerLimitTest(FakeRoot):
    """JupyterHub hands the single-user server its limits as MEM_LIMIT and CPU_LIMIT."""

    def fake_vm(self, total, available):
        return mock.patch.object(sysinfo.psutil, "virtual_memory", return_value=mock.Mock(total=total, available=available, percent=50.0))

    def test_sizes_and_limits(self):
        self.assertEqual(limits.parse_bytes("4294967296"), 4 * GIB)
        self.assertEqual(limits.parse_bytes("4G"), 4 * GIB)
        self.assertEqual(limits.parse_bytes("1.5GiB"), int(1.5 * GIB))
        self.assertEqual(limits.parse_bytes("512M"), 512 * 1024 ** 2)
        for bad in (None, "", "lots", "0", "-1"):
            self.assertIsNone(limits.parse_bytes(bad), bad)
        self.assertEqual(limits.spawner_limits({"MEM_LIMIT": "8G", "CPU_LIMIT": "2.5"}), {"memory": 8 * GIB, "cpu": 2.5})
        self.assertEqual(limits.spawner_limits({"CPU_LIMIT": "x"}), {"memory": None, "cpu": None})
        self.assertEqual(limits.spawner_limits({}), {"memory": None, "cpu": None})

    def test_mem_limit_with_the_containers_usage_counter(self):
        # The cgroup the container sees has no limit (the cap sits above it, out of sight), but counts the usage.
        self.v2()
        self.write("sys/fs/cgroup/memory.max", "max")
        self.write("sys/fs/cgroup/memory.current", str(3 * GIB))
        self.write("sys/fs/cgroup/memory.stat", f"inactive_file {GIB}\n")
        with self.fake_vm(512 * GIB, 300 * GIB):
            mem = sysinfo.memory(self.root, environ={"MEM_LIMIT": str(16 * GIB)})
        self.assertEqual((mem["total"], mem["available"]), (16 * GIB, 14 * GIB))
        self.assertEqual((mem["limit"]["kind"], mem["limit"]["source"]), ("spawner", "MEM_LIMIT"))
        self.assertEqual(mem["machine_total"], 512 * GIB)

    def test_mem_limit_without_cgroups_counts_the_users_processes(self):
        with self.fake_vm(512 * GIB, 300 * GIB), mock.patch.object(sysinfo, "user_rss", return_value=GIB):
            mem = sysinfo.memory(self.root, environ={"MEM_LIMIT": "4G"})
        self.assertEqual((mem["total"], mem["available"]), (4 * GIB, 3 * GIB))

    def test_the_smaller_of_cgroup_limit_and_mem_limit_counts(self):
        self.v2()
        self.write("sys/fs/cgroup/memory.max", str(8 * GIB))
        self.write("sys/fs/cgroup/memory.current", str(GIB))
        with self.fake_vm(512 * GIB, 300 * GIB):
            self.assertEqual(sysinfo.memory(self.root, environ={"MEM_LIMIT": str(32 * GIB)})["limit"]["kind"], "cgroup")
            self.assertEqual(sysinfo.memory(self.root, environ={"MEM_LIMIT": str(4 * GIB)})["limit"]["kind"], "spawner")

    def test_cpu_limit(self):
        effective = limits.effective_cpus(64, self.root, spawner_cpu=4.0)
        self.assertEqual((effective["cores"], effective["limited_by"], effective["spawner"]), (4.0, "spawner", 4.0))
        self.v2()
        self.write("sys/fs/cgroup/cpu.max", "200000 100000")
        self.assertEqual(limits.effective_cpus(64, self.root, spawner_cpu=4.0)["limited_by"], "quota")


class DiagnosticsTest(FakeRoot):
    def test_raw_inputs_and_no_secrets(self):
        self.v2("/kubepods/pod1")
        self.write("sys/fs/cgroup/kubepods/pod1/memory.max", "max")
        self.write("sys/fs/cgroup/kubepods/pod1/memory.current", "123")
        environ = {"MEM_LIMIT": "4G", "CUDA_VISIBLE_DEVICES": "0", "JUPYTERHUB_API_TOKEN": "secret-token", "HOME": "/home/u"}
        text = "\n".join(limits.diagnostics(self.root, environ))
        self.assertIn("MEM_LIMIT=4G", text)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", text)
        self.assertIn("memory.max=max memory.current=123", text)
        self.assertIn("# cgroup version: 2", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("HOME", text)


class EnvironmentTest(FakeRoot):
    def test_containers(self):
        self.assertIsNone(limits.container(self.root, environ={}))
        self.assertEqual(limits.container(self.root, environ={"KUBERNETES_SERVICE_HOST": "10.0.0.1"})["kind"], "Kubernetes")
        self.write(".dockerenv", "")
        self.assertEqual(limits.container(self.root, environ={})["kind"], "Docker")

    def test_containers_by_cgroup_and_overlay(self):
        self.write("proc/1/cgroup", "0::/system.slice/containerd.service\n")
        self.assertEqual(limits.container(self.root, environ={})["kind"], "containerd")
        self.write("proc/1/cgroup", "0::/\n")
        self.write("proc/self/mountinfo", "1234 1 0:52 / / rw,relatime - overlay overlay rw,lowerdir=/x\n")
        self.assertEqual(limits.container(self.root, environ={})["kind"], "container")
        self.write("run/.containerenv", "")
        self.assertEqual(limits.container(self.root, environ={})["kind"], "Podman")

    def test_virtual_machines(self):
        none = lambda args: None  # noqa: E731
        self.assertIsNone(limits.virtual_machine(none, self.root, system="Linux", release="6.8.0"))
        self.assertEqual(limits.virtual_machine(none, self.root, system="Linux", release="5.15.0-microsoft-standard-WSL2")["kind"], "WSL 2 (Hyper-V)")
        self.assertEqual(limits.virtual_machine(lambda args: "kvm\n", self.root, system="Linux", release="6.8")["kind"], "kvm")
        self.write("sys/class/dmi/id/sys_vendor", "QEMU")
        self.assertEqual(limits.virtual_machine(none, self.root, system="Linux", release="6.8")["kind"], "QEMU/KVM")
        (self.root / "sys/class/dmi/id/sys_vendor").unlink()
        self.write("proc/cpuinfo", "processor : 0\nflags : fpu vme hypervisor lahf_lm\n")
        self.assertEqual(limits.virtual_machine(none, self.root, system="Linux", release="6.8")["evidence"], "CPU flag hypervisor")
        self.assertEqual(limits.virtual_machine(lambda args: "1\n", self.root, system="Darwin")["kind"], "virtual machine")
        self.assertIsNone(limits.virtual_machine(lambda args: "0\n", self.root, system="Darwin"))

    def test_cpu_lists(self):
        self.assertEqual(limits.parse_cpu_list("0-3,6,8-9"), 7)
        self.assertEqual(limits.parse_cpu_list("5"), 1)
        for bad in (None, "", "a-b"):
            self.assertIsNone(limits.parse_cpu_list(bad), bad)


class VisibleGpuTest(unittest.TestCase):
    GPUS = sysinfo.parse_nvidia_smi("0, A, 100, 1, 99, 550, GPU-aaaa-1111\n1, B, 100, 1, 99, 550, GPU-bbbb-2222\n2, C, 100, 1, 99, 550, GPU-cccc-3333\n")

    def names(self, value):
        return [gpu["name"] for gpu in sysinfo.visible_gpus([dict(g) for g in self.GPUS], {"CUDA_VISIBLE_DEVICES": value})]

    def test_cuda_visible_devices(self):
        self.assertEqual([g["name"] for g in sysinfo.visible_gpus(list(self.GPUS), {})], ["A", "B", "C"])
        self.assertEqual(self.names("2,0"), ["C", "A"])
        self.assertEqual(self.names("GPU-bbbb"), ["B"])
        self.assertEqual(self.names("bbbb"), ["B"])
        self.assertEqual(self.names("1,7,2"), ["B"], "CUDA stops at the first entry that matches nothing")
        self.assertEqual(self.names(""), [])
        self.assertEqual(self.names("-1"), [])


if __name__ == "__main__":
    unittest.main()
