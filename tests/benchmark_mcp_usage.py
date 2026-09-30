"""Repeatable LOCAL-ONLY usage experiment; no models, accounts or paid endpoints.

Run on Linux/WSL: python3 -B tests/benchmark_mcp_usage.py --seconds 10 --history 10000 --burst 100
Measurements include the two-owner Python test harness/callback receiver and
its two Herald daemon children. They are not whole-host or standalone-bridge
figures. Fixture setup, subscription verification and initial history indexing
are reported/excluded from steady-state samples. All sockets are loopback-only.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import resource
import socket
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_mcp as fixtures
from test_mcp_usage import history


def process_snapshot(pids):
    cpu = {}
    rss = 0
    io = {k: 0 for k in ("rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw")}
    for pid in pids:
        root = Path("/proc") / str(pid)
        try:
            fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
            cpu[pid] = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
            status = (root / "status").read_text()
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    rss += int(line.split()[1]) * 1024
            for line in (root / "io").read_text().splitlines():
                key, value = line.split(":", 1)
                if key in io:
                    io[key] += int(value)
        except (OSError, ValueError):
            continue
    return {"cpu": cpu, "rss": rss, "io": io}


def fixture():
    f = fixtures.MCPIntegration(methodName="test_greeting_signed_event_and_threaded_reply")
    f.setUp()
    return f


def run_scenario(name, seconds, history_size, burst_size, connections):
    f = fixture()
    try:
        b = f.bridges["simon"]
        pids = [os.getpid()] + [p.pid for p in f.processes]
        index_seconds = 0
        if name == "retained_history_idle":
            history(b, history_size)
        if name != "no_subscriptions":
            policy = json.loads(f.configs["simon"].read_text())
            policy["event_limit"] = max(10, burst_size)
            f.configs["simon"].write_text(json.dumps(policy))
            f.subscribe()
            start = time.perf_counter()
            f.tick()
            index_seconds = time.perf_counter() - start
        if name == "retry_duplicate":
            post = b.transport.post
            lost = [False]
            def lose_ack(*args):
                status, response = post(*args)
                if not lost[0]:
                    lost[0] = True
                    raise OSError("Simulated lost acknowledgement AFTER receiver accepted the event")
                return status, response
            b.transport.post = lose_ack
        before = process_snapshot(pids)
        cpu_start = time.process_time()
        counters = dict(b.counts)
        own_sender_counts = dict(f.bridges["jamie"].counts)
        changes = b.db.total_changes
        callbacks = len(f.callbacks)
        connects = connections[0]
        peak_rss = before["rss"]
        started = time.perf_counter()
        ticks = 0
        while time.perf_counter() - started < seconds:
            if ticks == 0:
                if name == "message_burst":
                    for index in range(burst_size):
                        f.send("usage-burst-" + str(index))
                if name == "retry_duplicate":
                    f.send("usage-retry")
                    f.send("usage-retry")  # Same write key; not a second message.
            f.tick()
            ticks += 1
            peak_rss = max(peak_rss, process_snapshot(pids)["rss"])
            remaining = min(started + seconds, started + ticks) - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        elapsed = time.perf_counter() - started
        cpu_seconds = time.process_time() - cpu_start
        after = process_snapshot(pids)
        child_cpu = sum(after["cpu"].get(pid, 0) - before["cpu"].get(pid, 0) for pid in pids[1:])
        delta = {key: value - counters[key] for key, value in b.counts.items()}
        sender = {key: value - own_sender_counts[key] for key, value in f.bridges["jamie"].counts.items()}
        idle = name in ("no_subscriptions", "idle_subscribed", "retained_history_idle")
        if idle:
            assert delta["inbox_files_parsed"] == delta["callback_attempts"] == delta["sqlite_commits"] == 0
            assert b.db.total_changes == changes
            assert connections[0] == connects
        if name == "message_burst":
            assert len(f.seen) == burst_size and delta["callback_attempts"] == burst_size
        if name == "retry_duplicate":
            assert len(f.seen) == 1 and delta["callback_attempts"] == 2 and delta["callback_retries"] == 1
            assert sender["idempotent_write_hits"] == 1
        assert b.counts["reply_calls"] == b.counts["send_calls"] == 0
        return {"scenario": name, "wall_seconds": elapsed, "ticks": ticks,
                "fixture_history_records": history_size if name == "retained_history_idle" else 0,
                "fixture_new_messages": burst_size if name == "message_burst" else 1 if name == "retry_duplicate" else 0,
                "initial_index_seconds_excluded": index_seconds,
                "harness_and_receiver_cpu_seconds": cpu_seconds,
                "daemon_children_cpu_seconds": child_cpu,
                "aggregate_cpu_percent_of_one_core": (cpu_seconds + child_cpu) / elapsed * 100,
                "sampled_sum_rss_peak_bytes": peak_rss, "sampled_sum_rss_end_bytes": after["rss"],
                "harness_os_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                "process_io_delta": {key: after["io"][key] - before["io"][key] for key in before["io"]},
                "loopback_connections": connections[0] - connects,
                "receiver_callback_posts": len(f.callbacks) - callbacks,
                "receiver_unique_event_effects": len(f.seen), "bridge_counters_delta": delta,
                "sender_counters_delta": sender, "sqlite_row_changes": b.db.total_changes - changes,
                "bridge_model_calls": 0, "autonomous_replies": 0,
                "actual_chatgpt_tokens_and_credits": "NOT_MEASURED_NO_LIVE_INTEGRATION"}
    finally:
        f.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--history", type=int, default=10000)
    parser.add_argument("--burst", type=int, default=100)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not 5 <= args.seconds <= 60 or not 0 <= args.history <= 100000 or not 1 <= args.burst <= 1000:
        parser.error("Use 5-60 seconds, 0-100000 history records and 1-1000 burst messages")
    if args.burst > 16 * args.seconds:
        parser.error("Burst must fit the 16-callbacks-per-tick measurement window")
    original_connect, original_resolve = socket.create_connection, socket.getaddrinfo
    connections = [0]
    def guarded_connect(address, *pos, **kw):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError("Benchmark forbids every non-loopback connection")
        connections[0] += 1
        return original_connect(address, *pos, **kw)
    def guarded_resolve(host, *pos, **kw):
        if not ipaddress.ip_address(host).is_loopback:
            raise AssertionError("Benchmark forbids external DNS destinations")
        return original_resolve(host, *pos, **kw)
    cpu_model = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                      if line.startswith("model name")), "unknown")
    result = {"environment": {"python": platform.python_version(), "kernel": platform.release(),
                               "platform": platform.system(), "cpu_model": cpu_model, "logical_cpus": os.cpu_count(),
                               "fixture_storage": "Linux tempfile directories under /tmp; code loaded from the repository",
                               "measurement_scope": "Python two-owner test harness + signed callback receiver + two real Herald daemon children",
                               "io_caveat": "rchar/syscr include /proc measurement reads; read_bytes/write_bytes are kernel-reported physical I/O, affected by cache and delayed writeback",
                               "memory_caveat": "1 Hz sampled sum of process RSS can double-count shared pages; not whole-host memory or standalone bridge memory",
                               "network_caveat": "Loopback fixture connections only; payload-byte counters exclude HTTP/TCP headers; no paid/model endpoints permitted"},
              "samples": []}
    with patch("socket.create_connection", guarded_connect), patch("socket.getaddrinfo", guarded_resolve):
        for name in ("no_subscriptions", "idle_subscribed", "retained_history_idle", "message_burst", "retry_duplicate"):
            sample = run_scenario(name, args.seconds, args.history, args.burst, connections)
            result["samples"].append(sample)
            print(json.dumps(sample), flush=True)
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"environment": result["environment"], "all_assertions_passed": True}), flush=True)


if __name__ == "__main__":
    main()
