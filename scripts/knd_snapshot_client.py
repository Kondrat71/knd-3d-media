"""Prepare a local recipient or decode one read-only KND Actions run.

python scripts/knd_snapshot_client.py prepare /tmp/knd-recipient
python scripts/knd_snapshot_client.py decode /tmp/knd-recipient run.log 123 live.json

Commit only recipient.pem to analytics/recipient.pem on main. This triggers
the snapshot for the public certificate. Never commit or print key.pem.
Supply text logs from that run's read job to decode, including KND_ENC lines.
"""
import argparse
import base64
import gzip
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def prepare(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes",
        "-days", "4", "-subj", "/CN=KND analytics recipient",
        "-keyout", str(directory / "key.pem"), "-out", str(directory / "recipient.pem")
    ], check=True, capture_output=True)
    (directory / "key.pem").chmod(0o600)


def decode(directory, logs, run_id, target):
    lines = re.findall(r"^(?:\d{4}-\S+ )?KND_ENC:([A-Za-z0-9+/=]+)\s*$",
                       logs.read_text(), flags=re.MULTILINE)
    if not lines:
        raise ValueError("No encrypted output found in these logs")
    encrypted = base64.b64decode("".join(lines), validate=True)
    decrypted = subprocess.run([
        "openssl", "cms", "-decrypt", "-binary", "-inform", "DER", "-recip",
        str(directory / "recipient.pem"), "-inkey", str(directory / "key.pem")
    ], input=encrypted, capture_output=True, check=True)
    data = json.loads(gzip.decompress(decrypted.stdout))
    if not {"asof", "periods", "failures", "finance"} <= data.keys():
        raise ValueError("Snapshot missing required fields")
    data["run_id"] = run_id
    with tempfile.NamedTemporaryFile("w", dir=target.parent, encoding="utf-8", delete=False) as f:
        name = f.name
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("prepare")
    init.add_argument("directory", type=Path)
    unpack = commands.add_parser("decode")
    unpack.add_argument("directory", type=Path)
    unpack.add_argument("logs", type=Path)
    unpack.add_argument("run_id", type=int)
    unpack.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.directory)
    else:
        decode(args.directory, args.logs, args.run_id, args.output)
