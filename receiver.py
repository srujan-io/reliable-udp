import argparse
import hashlib
import json
import os
import random
import socket
import struct
import time
from pathlib import Path

CHUNK_SIZE = 1200


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def ack(sock, address, kind, seq):
    sock.sendto(b"A" + kind + struct.pack("!I", seq), address)


def valid_metadata(meta):
    if not isinstance(meta, dict):
        return False
    if not isinstance(meta.get("filename"), str) or not meta["filename"]:
        return False
    if not isinstance(meta.get("size"), int) or isinstance(meta["size"], bool) or meta["size"] < 0:
        return False
    if not isinstance(meta.get("total_packets"), int) or isinstance(meta["total_packets"], bool):
        return False
    if not isinstance(meta.get("sha256"), str) or len(meta["sha256"]) != 64:
        return False
    try:
        int(meta["sha256"], 16)
    except ValueError:
        return False
    return meta["total_packets"] == (meta["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE


def main():
    parser = argparse.ArgumentParser(description="Multi-client reliable UDP receiver")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", default="received_files")
    parser.add_argument("--idle-timeout", type=float, default=120.0)
    parser.add_argument("--linger", type=float, default=15.0)
    parser.add_argument("--once", action="store_true", help="Exit after one completed transfer and linger")
    args = parser.parse_args()
    if not 0 <= args.loss < 1 or args.idle_timeout <= 0 or args.linger <= 0:
        parser.error("Invalid loss, idle timeout, or linger value")
    rng = random.Random(args.seed)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sessions = {}
    completed_count = 0
    print(f"Reliable UDP receiver listening on port {args.port}", flush=True)
    print(f"Simulated packet loss: {args.loss * 100:.0f}%", flush=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((args.host, args.port))
        sock.settimeout(0.5)
        try:
            while True:
                now = time.monotonic()
                for address, session in list(sessions.items()):
                    age = now - session["last_seen"]
                    limit = args.linger if session["complete"] else args.idle_timeout
                    if age > limit:
                        if not session["complete"]:
                            print(f"[TIMEOUT] Incomplete transfer from {address}", flush=True)
                        del sessions[address]
                if args.once and completed_count and not sessions:
                    break
                try:
                    packet, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                if not packet:
                    continue
                kind = packet[:1]
                session = sessions.get(address)
                if kind == b"M":
                    try:
                        meta = json.loads(packet[1:].decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not valid_metadata(meta):
                        continue
                    if session is None:
                        if args.once and completed_count:
                            continue
                        session = {"meta": meta, "chunks": {}, "last_seen": now,
                                   "complete": False, "drops": 0, "duplicates": 0,
                                   "arrivals": 0, "output": None}
                        sessions[address] = session
                        print(f"Receiving {meta['filename']}: {meta['size']} bytes "
                              f"from {address}; expected packets: {meta['total_packets']}", flush=True)
                    if session["meta"] == meta:
                        session["last_seen"] = now
                        ack(sock, address, b"M", 0)
                    continue
                if session is None:
                    continue
                session["last_seen"] = now
                meta = session["meta"]
                if kind == b"D" and len(packet) >= 5 and not session["complete"]:
                    seq = struct.unpack("!I", packet[1:5])[0]
                    if seq >= meta["total_packets"]:
                        continue
                    payload = packet[5:]
                    expected_len = min(CHUNK_SIZE, meta["size"] - seq * CHUNK_SIZE)
                    if len(payload) != expected_len:
                        continue
                    session["arrivals"] += 1
                    if rng.random() < args.loss:
                        session["drops"] += 1
                        continue
                    if seq in session["chunks"]:
                        session["duplicates"] += 1
                    else:
                        session["chunks"][seq] = payload
                    ack(sock, address, b"D", seq)
                    continue
                if kind == b"E" and len(packet) == 5:
                    seq = struct.unpack("!I", packet[1:])[0]
                    if seq != meta["total_packets"]:
                        continue
                    if session["complete"]:
                        ack(sock, address, b"E", seq)
                        continue
                    if len(session["chunks"]) != meta["total_packets"]:
                        continue
                    # Unique filename per client to avoid simultaneous writes overwriting each other.
                    basename = os.path.basename(meta["filename"].replace("\\", "/"))
                    output = output_dir / f"{address[0].replace('.', '_')}_{address[1]}_{basename}"
                    with open(output, "wb") as f:
                        for i in range(meta["total_packets"]):
                            f.write(session["chunks"][i])
                    digest = sha256_file(output)
                    size = output.stat().st_size
                    ok = size == meta["size"] and digest == meta["sha256"]
                    print("\n========== RECEIVER REPORT ==========")
                    print(f"Saved: {output}")
                    print(f"Packets received: {len(session['chunks'])}/{meta['total_packets']}")
                    print(f"Total data arrivals: {session['arrivals']}")
                    print(f"Simulated drops: {session['drops']}")
                    print(f"Duplicate packets: {session['duplicates']}")
                    print(f"Expected size: {meta['size']} bytes")
                    print(f"Received size: {size} bytes")
                    print(f"SHA-256: {digest}")
                    print("SUCCESS: File integrity verified." if ok else "FAILURE: File integrity mismatch.")
                    print("=====================================", flush=True)
                    if ok:
                        session["complete"] = True
                        session["chunks"].clear()
                        session["last_seen"] = time.monotonic()
                        completed_count += 1
                        ack(sock, address, b"E", seq)
                    else:
                        del sessions[address]
        except KeyboardInterrupt:
            print("Receiver stopped.", flush=True)


if __name__ == "__main__":
    main()
