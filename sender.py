import argparse
import hashlib
import json
import os
import select
import socket
import struct
import time

CHUNK_SIZE = 1200


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def wait_for_ack(sock, destination, expected_type, expected_seq, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            return False
        packet, address = sock.recvfrom(2048)
        if (address == destination and len(packet) == 6
                and packet[:2] == b"A" + expected_type
                and struct.unpack("!I", packet[2:])[0] == expected_seq):
            return True


def reliable_control(sock, destination, packet, ack_type, ack_seq, timeout, retries):
    for attempt in range(retries + 1):
        sock.sendto(packet, destination)
        if wait_for_ack(sock, destination, ack_type, ack_seq, timeout):
            return attempt
        if attempt < retries:
            print(f"Control packet timeout; retry {attempt + 1}/{retries}", flush=True)
    raise TimeoutError(f"Receiver did not acknowledge {ack_type!r} control packet")


def transfer(args):
    path = args.file
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    size = os.path.getsize(path)
    count = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    if count > 0xFFFFFFFF:
        raise ValueError("File exceeds sequence-number limit")
    metadata = {"filename": os.path.basename(path), "size": size,
                "sha256": sha256_file(path), "total_packets": count}
    destination = (socket.gethostbyname(args.host), args.port)
    print(f"Sending: {metadata['filename']}\nFile size: {size} bytes\n"
          f"Total packets: {count}\nWindow size: {args.window}", flush=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", 0))
        reliable_control(sock, destination, b"M" + json.dumps(metadata).encode(),
                         b"M", 0, args.timeout, args.retries)
        with open(path, "rb") as f:
            chunks = [f.read(CHUNK_SIZE) for _ in range(count)]
        base = next_seq = retransmissions = 0
        acknowledged = set()
        sent_at = {}
        retries = {}
        last_reported = 0
        started = time.monotonic()
        while base < count:
            while next_seq < count and next_seq < base + args.window:
                sock.sendto(b"D" + struct.pack("!I", next_seq) + chunks[next_seq], destination)
                sent_at[next_seq] = time.monotonic()
                retries[next_seq] = 0
                next_seq += 1
            ready, _, _ = select.select([sock], [], [], 0.025)
            if ready:
                packet, address = sock.recvfrom(2048)
                if address == destination and len(packet) == 6 and packet[:2] == b"AD":
                    seq = struct.unpack("!I", packet[2:])[0]
                    if base <= seq < next_seq:
                        acknowledged.add(seq)
                        sent_at.pop(seq, None)
                        while base in acknowledged:
                            acknowledged.remove(base)
                            base += 1
            now = time.monotonic()
            for seq in list(sent_at):
                if now - sent_at[seq] < args.timeout:
                    continue
                if retries[seq] >= args.retries:
                    raise TimeoutError(f"Packet {seq} failed after {args.retries} retries")
                sock.sendto(b"D" + struct.pack("!I", seq) + chunks[seq], destination)
                retries[seq] += 1
                retransmissions += 1
                sent_at[seq] = now
                print(f"[RETRANSMIT] Packet {seq} (retry {retries[seq]})", flush=True)
            if base - last_reported >= max(args.window, 1):
                print(f"Progress: {base}/{count} packets acknowledged", flush=True)
                last_reported = base
        end_retries = reliable_control(sock, destination,
                                       b"E" + struct.pack("!I", count),
                                       b"E", count, args.timeout, args.retries)
        elapsed = time.monotonic() - started
    report = {"success": True, "bytes": size, "packets": count,
              "retransmissions": retransmissions, "end_retries": end_retries,
              "seconds": round(elapsed, 4), "sha256": metadata["sha256"],
              "throughput_mbps": round(size * 8 / max(elapsed, 1e-9) / 1e6, 4)}
    print("\n========== SENDER REPORT ==========")
    print(f"Packets acknowledged: {count}/{count}")
    print(f"Retransmissions: {retransmissions}")
    print(f"Transfer time: {elapsed:.4f} seconds")
    print(f"Original SHA-256: {metadata['sha256']}")
    print("Transfer completed.\n===================================")
    print("METRICS_JSON=" + json.dumps(report, sort_keys=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="Reliable UDP file sender")
    parser.add_argument("file")
    parser.add_argument("--host", default="10.0.0.2")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--window", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.window <= 65536 or args.timeout <= 0 or args.retries < 0:
        parser.error("Invalid window, timeout, or retry count")
    try:
        transfer(args)
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"TRANSFER FAILED: {exc}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
