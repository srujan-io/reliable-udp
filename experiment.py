
"""Run from Ubuntu:
sudo /usr/bin/python3 -u experiment.py --scenario normal|loss20|failover|concurrent

OS-Ken controller must already be running on port 6653.
Do not start another Mininet instance.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import Link
from mininet.log import setLogLevel

from topology import ReliableUDPTopology


ROOT = Path(__file__).resolve().parent

FIELDS = [
    "scenario",
    "mode",
    "success",
    "bytes",
    "seconds",
    "throughput_mbps",
    "retransmissions",
    "packets",
    "sha256_verified",
    "link_failure_triggered",
    "error",
]


def make_test_file(path, mb):
    expected_size = mb * 1024 * 1024

    if not path.exists() or path.stat().st_size != expected_size:
        with open(path, "wb") as f:
            for _ in range(mb):
                f.write(os.urandom(1024 * 1024))


def file_hash(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)

    return h.hexdigest()


def start_process(host, argv, log_path):
    handle = open(log_path, "w")

    proc = host.popen(
        argv,
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )

    return proc, handle


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--scenario",
        choices=["normal", "loss20", "failover", "concurrent"],
        required=True,
    )

    ap.add_argument(
        "--mode",
        choices=["adaptive", "static"],
        default="adaptive",
        help="Must match SDN_MODE used to start the controller",
    )

    ap.add_argument(
        "--size-mb",
        type=int,
        default=10,
    )

    ap.add_argument(
        "--fail-after",
        type=float,
        default=4.0,
    )

    # Increased from 120 seconds to 900 seconds.
    ap.add_argument(
        "--timeout",
        type=float,
        default=900.0,
    )

    args = ap.parse_args()

    if args.size_mb < 1 or args.timeout <= 0:
        ap.error("size-mb and timeout must be positive")

    setLogLevel("warning")

    results = ROOT / "results"
    results.mkdir(exist_ok=True)

    test_file = ROOT / "large_test.bin"
    make_test_file(test_file, args.size_mb)

    received_dir = results / "received"
    received_dir.mkdir(exist_ok=True)

    # Remove previous received files for this test.
    for stale in received_dir.glob("10_0_0_1_*_large_test.bin"):
        stale.unlink()

    net = Mininet(
        topo=ReliableUDPTopology(),
        controller=None,
        switch=OVSSwitch,
        link=Link,
        autoSetMacs=True,
    )

    net.addController(
        "c0",
        controller=RemoteController,
        ip="127.0.0.1",
        port=6653,
    )

    procs = []
    handles = []
    failure = False

    row = {k: "" for k in FIELDS}

    row.update(
        scenario=args.scenario,
        mode=args.mode,
        bytes=test_file.stat().st_size,
    )

    try:
        net.start()

        h1, h2 = net.get("h1", "h2")

        time.sleep(1.5)

        # Keep the bandwidth limit consistent for
        # normal, loss, and failover experiments.
        if args.scenario != "concurrent":
            h1.cmd(
                "tc qdisc replace dev h1-eth0 root tbf "
                "rate 2mbit burst 32kb latency 400ms"
            )

        loss = "0.20" if args.scenario == "loss20" else "0.0"

        receiver, rf = start_process(
            h2,
            [
                sys.executable,
                "-u",
                str(ROOT / "receiver.py"),
                "--loss",
                loss,
                "--seed",
                "42",
                "--output-dir",
                str(received_dir),
            ],
            results / f"{args.scenario}_{args.mode}_receiver.log",
        )

        procs.append(receiver)
        handles.append(rf)

        time.sleep(0.4)

        sender_count = 2 if args.scenario == "concurrent" else 1

        senders = []

        for i in range(sender_count):
            proc, handle = start_process(
                h1,
                [
                    sys.executable,
                    "-u",
                    str(ROOT / "sender.py"),
                    str(test_file),
                    "--host",
                    "10.0.0.2",
                ],
                results
                / f"{args.scenario}_{args.mode}_sender{i + 1}.log",
            )

            senders.append(proc)
            procs.append(proc)
            handles.append(handle)

        started = time.monotonic()

        # Simulate failure while the file transfer is active.
        if args.scenario == "failover":
            time.sleep(args.fail_after)

            if any(p.poll() is None for p in senders):
                net.configLinkStatus("s2", "s4", "down")
                failure = True

                print(
                    "PRIMARY LINK DOWN: s2-s4",
                    flush=True,
                )

        # One overall timeout for the entire experiment.
        deadline = started + args.timeout

        for proc in senders:
            remaining = max(
                0.1,
                deadline - time.monotonic(),
            )

            proc.wait(timeout=remaining)

        for handle in handles:
            handle.flush()

        reports = []

        for i, proc in enumerate(senders):
            log_path = (
                results
                / f"{args.scenario}_{args.mode}_sender{i + 1}.log"
            )

            log = log_path.read_text()

            matches = re.findall(
                r"^METRICS_JSON=(.+)$",
                log,
                flags=re.M,
            )

            if proc.returncode != 0 or not matches:
                raise RuntimeError(
                    f"Sender {i + 1} failed; inspect {log_path}"
                )

            report = json.loads(matches[-1])
            reports.append(report)

        receiver_log_path = (
            results
            / f"{args.scenario}_{args.mode}_receiver.log"
        )

        # Give the receiver a moment to finish writing its report.
        integrity_deadline = time.monotonic() + 5

        while time.monotonic() < integrity_deadline:
            for handle in handles:
                handle.flush()

            receiver_log = receiver_log_path.read_text()

            received = list(
                received_dir.glob("10_0_0_1_*_large_test.bin")
            )

            correct = sum(
                file_hash(f) == reports[0]["sha256"]
                for f in received
            )

            successful_reports = receiver_log.count(
                "SUCCESS: File integrity verified."
            )

            if (
                correct >= sender_count
                and successful_reports >= sender_count
            ):
                break

            time.sleep(0.2)

        if (
            correct < sender_count
            or successful_reports < sender_count
        ):
            raise RuntimeError(
                "Receiver integrity verification incomplete"
            )

        # Correctly account for both senders in concurrent mode.
        total_bytes = sum(
            report["bytes"] for report in reports
        )

        total_packets = sum(
            report["packets"] for report in reports
        )

        total_retransmissions = sum(
            report["retransmissions"] for report in reports
        )

        transfer_seconds = max(
            report["seconds"] for report in reports
        )

        throughput_mbps = (
            total_bytes * 8
            / max(transfer_seconds, 1e-9)
            / 1_000_000
        )

        row.update(
            success=True,
            bytes=total_bytes,
            seconds=round(transfer_seconds, 4),
            throughput_mbps=round(throughput_mbps, 4),
            retransmissions=total_retransmissions,
            packets=total_packets,
            sha256_verified=True,
        )

    except Exception as exc:
        row.update(
            success=False,
            sha256_verified=False,
            error=str(exc),
        )

        print(
            "EXPERIMENT FAILED:",
            exc,
            flush=True,
        )

    finally:
        row["link_failure_triggered"] = failure

        for proc in procs:
            if proc.poll() is None:
                proc.terminate()

        for proc in procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        for handle in handles:
            handle.close()

        net.stop()

        csv_path = results / "metrics.csv"
        exists = csv_path.exists()

        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=FIELDS,
            )

            if not exists:
                writer.writeheader()

            writer.writerow(row)

        print(
            "RESULT:",
            json.dumps(row, sort_keys=True),
            flush=True,
        )

        print(
            "CSV:",
            csv_path,
            flush=True,
        )

    if row["success"] is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
