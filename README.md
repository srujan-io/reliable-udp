# Reliable UDP File Transfer with SDN-Based Adaptive Rerouting

Computer Networks / SDN-Socket Programming mini-project. Python UDP application transfers files reliably over a Mininet topology programmed by an OS-Ken OpenFlow 1.3 controller. Primary-path failures trigger automatic rerouting through a redundant path.

## Architecture

```text
H1 (10.0.0.1) -- S1 -- S2 -- S4 -- H2 (10.0.0.2)
                    \        /
                     S3 -----
```

- Primary: H1–S1–S2–S4–H2.
- Backup: H1–S1–S3–S4–H2.
- S2–S4 failure is detected via OpenFlow `EventOFPPortStatus`; controller updates forwarding rules.
- UDP reliability: metadata handshake, numbered 1200-byte chunks, window of 16, selective acknowledgments, retransmission on timeout, final handshake, SHA-256 verification.
- Receiver supports multiple clients distinguished by `(source IP, source UDP port)`; outputs include client identifier to avoid file collisions.
- Receiver stays running between transfers and keeps completed sessions briefly to re-ACK duplicate end packets.

## Requirements

Ubuntu/WSL2, Python 3, Mininet, Open vSwitch, `tc` (iproute2), and OS-Ken in a virtual environment. Mininet must be able to use OpenFlow 1.3. Run OS-Ken as your regular user; run Mininet as root.

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-venv iproute2
python3 -m venv .venv
source .venv/bin/activate
pip install os-ken
```

## Interactive demonstration

Open **Terminal 1** in the project directory:

```bash
source .venv/bin/activate
SDN_MODE=adaptive osken-manager --ofp-tcp-listen-port 6653 controller.py
```

Open **Terminal 2**:

```bash
sudo mn -c
sudo mn --custom topology.py --topo reliableudp --controller remote,ip=127.0.0.1,port=6653 --switch ovsk,protocols=OpenFlow13
```

At the `mininet>` prompt (each command is a separate line):

```text
h1 ping -c 3 h2
sh dd if=/dev/urandom of=large_test.bin bs=1M count=10
h1 tc qdisc replace dev h1-eth0 root tbf rate 2mbit burst 32kb latency 400ms
h2 python3 -u receiver.py --loss 0 --output-dir received_files > /tmp/receiver_demo.log 2>&1 &
h1 python3 -u sender.py large_test.bin --host 10.0.0.2 > /tmp/sender_demo.log 2>&1 &
sh tail -5 /tmp/sender_demo.log
link s2 s4 down
sh ovs-ofctl -O OpenFlow13 dump-flows s1
sh tail -12 /tmp/sender_demo.log
sh tail -15 /tmp/receiver_demo.log
link s2 s4 up
sh ovs-ofctl -O OpenFlow13 dump-flows s1
h1 tc qdisc del dev h1-eth0 root
```

Break the link while sender progress is visible. `s1-eth1 -> s1-eth3` confirms backup; after restoring the link, `s1-eth1 -> s1-eth2` confirms primary. The receiver output includes the verified SHA-256. Do not launch a second sender while an earlier transfer is running unless intentionally testing concurrency.

## Automated experiments

**Stop the interactive Mininet instance first** (`exit`), then `sudo mn -c`. Keep the controller running in Terminal 1. Run these commands from a normal Ubuntu shell, not `mininet>`:

```bash
sudo python3 experiment.py --scenario normal --mode adaptive
sudo python3 experiment.py --scenario loss20 --mode adaptive
sudo python3 experiment.py --scenario failover --mode adaptive
sudo python3 experiment.py --scenario concurrent --mode adaptive
```

The runner creates a 10 MiB input file if necessary, starts a fresh Mininet network for each scenario, sets 2 Mbps sender bandwidth (except the concurrency demonstration), launches a receiver and one or two senders, injects S2–S4 failure 4 seconds after starting the sender in the failover scenario, and writes logs and `results/metrics.csv`. Failure injection is recorded only if the sender is still running. Verify controller logs show `ACTIVE SDN PATH: BACKUP` to confirm actual rerouting. The `failover` run may still succeed with zero retransmissions if no packet was in flight during the transition.

To compare **static forwarding** fairly, stop the controller and restart it in Terminal 1 as:

```bash
SDN_MODE=static osken-manager --ofp-tcp-listen-port 6653 controller.py
```

Then run:

```bash
sudo mn -c
sudo python3 experiment.py --scenario failover --mode static --timeout 40
```

A static-controller failure is an expected outcome when S2–S4 is down; the CSV records `success=False`. Do not label it as an adaptive failure. Use the same file, bandwidth, timeout and failure schedule for comparisons. For repeatability, run each scenario at least 3 times, noting system load and variability.

### Metrics

- **Transfer duration:** sender's data phase through end-of-transfer ACK; excludes metadata negotiation and file hashing.
- **Goodput (Mbps):** `file_bytes * 8 / transfer_seconds / 1e6`; measures application payload delivery rate, not link-layer throughput.
- **Retransmissions:** number of retransmitted data packets, excluding metadata and end control retries (reported separately as `end_retries`).
- **Integrity:** source and received SHA-256 match.
- **Failover detection:** controller logs indicate event and path change. Precise failover latency is **not** measured by the current runner; don't claim a measured recovery time without additional instrumentation.
- **Loss20:** deterministic simulated drop decisions in the receiver; not physical link packet loss.

## Protocol

| Type | Format | Description |
|---|---|---|
| Metadata | `M` + JSON | filename, byte count, SHA-256, total packets |
| Data | `D` + 4-byte big-endian sequence + payload | up to 1200 payload bytes |
| End | `E` + 4-byte sequence | sender requests transfer completion |
| ACK | `A` + packet type (`M`, `D`, or `E`) + 4-byte sequence | confirms receipt |

Each sender uses an ephemeral source port. The receiver identifies each transfer by the UDP source address and port. It accepts repeated metadata packets for an existing session and acknowledges repeated end packets during a configurable linger period.

## Viva talking points

1. UDP offers no built-in delivery guarantee; sequence numbers, ACKs, timeout and retransmission add reliability at application layer.
2. A 16-packet sliding window improves utilization over stop-and-wait; cumulative progress is advanced only through consecutively acknowledged sequences.
3. Mininet emulates hosts, switches, and links; Open vSwitch forwards using rules installed by the OS-Ken controller over OpenFlow 1.3.
4. The controller handles port-status notifications for the S2–S4 link and reprograms S1, S3, S4 to use the alternate path; link restoration switches back.
5. SHA-256 detects corrupted/incomplete files; the final ACK is sent only after the receiver verifies integrity.
6. Static forwarding cannot recover from the broken primary route; adaptive SDN can restore reachability while UDP retransmits missing application packets.

## Limitations

- Prototype assumes the specified four-switch topology and port numbering; it is not a general shortest-path SDN controller.
- Link-status detection covers S2–S4 only; other failures and congestion-based rerouting are not implemented.
- The receiver holds chunks in memory until transfer completion; large files require substantial RAM.
- Sender's retry budget is finite; severe congestion or long failures may still cause transfer failure.
- Receiver uses a source-port-based filename and keeps completed sessions only temporarily; no persistent transfer ID or resume after restart.
- No authentication, encryption, congestion-control algorithm, or adversarial-input hardening. Use in a controlled lab network only.

## Prior verified manual result

A 10 MiB live-failover test completed with 8,739/8,739 packets acknowledged, 3 retransmissions, 43.70 seconds and matching SHA-256 (`cd12b023775073f2d89d9be718f28630cfa7cc5f5dd6105ac8dec244a62b863e`). This was measured with the earlier implementation; rerun the updated code before attributing results to this version.
