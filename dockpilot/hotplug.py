"""Host-side USB insertion handler. Requires root, rsync and a trusted UUID allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from .cli import ValidationError, notify_ntfy, preview, read_yaml

DEVICE_RE = re.compile(r"^/dev/[a-zA-Z0-9_-]+$")
HOST_CONFIG = Path("/etc/dockpilot/config.yaml")


def run(args: list[str], **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def uuid_for_device(device: str) -> str:
    result = run(["blkid", "-s", "UUID", "-o", "value", device], capture_output=True, text=True)
    uuid = result.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{4,100}", uuid):
        raise ValidationError("Device has no valid filesystem UUID")
    return uuid


def notify(config: dict, title: str, message: str) -> None:
    try:
        notify_ntfy(config, title, message)
    except Exception as exc:
        print(f"Notification warning: {exc}", file=sys.stderr)


def check_storage(dest: Path, bytes_needed: int) -> None:
    # Reserve headroom for the subsequent Immich-managed copy and derivatives.
    free = shutil.disk_usage(dest).free
    need = bytes_needed * 2 + 20 * 1024**3
    if free < need:
        raise ValidationError(f"Not enough disk space; need {need // 1024**3} GiB, available {free // 1024**3} GiB")


def process(device: str, config_path: Path) -> None:
    if not DEVICE_RE.fullmatch(device) or not Path(device).is_block_device():
        raise ValidationError("Invalid block device")
    config = read_yaml(config_path)
    host = config.get("host", {})
    if not isinstance(host, dict):
        raise ValidationError("Missing host settings")
    uuid = uuid_for_device(device)
    trusted = host.get("trusted_uuids", [])
    if not isinstance(trusted, list) or uuid not in trusted:
        print(f"Ignoring unapproved drive with UUID {uuid}")
        return

    base = Path(host.get("shared_root", "/mnt/pve/media-hdd/dockpilot"))
    if not base.is_absolute() or not base.is_dir() or not os.path.ismount(base.parent):
        raise ValidationError("Shared root must exist on a mounted HDD")
    mount_root = Path(host.get("mount_root", "/mnt/dockpilot-drives"))
    if not mount_root.is_absolute():
        raise ValidationError("mount_root must be absolute")
    mount_root.mkdir(parents=True, exist_ok=True)
    drive = mount_root / uuid
    drive.mkdir(exist_ok=True)

    lock_dir = Path("/run/lock/dockpilot")
    lock_dir.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (lock_dir / (uuid + ".lock")).open("w") as lock:
        if not fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB) is None:
            pass
        mounted_here = False
        try:
            if not os.path.ismount(drive):
                run(["mount", "-t", "auto", "-o", "ro,nosuid,nodev,noexec", device, str(drive)])
                mounted_here = True
            if not os.path.ismount(drive):
                raise ValidationError("USB mount did not succeed")

            # Deny all untrusted manifests. The server controls approved actions/targets.
            info = preview(str(drive), str(config_path))
            if not info["jobs"]:
                print("No enabled import jobs")
                return
            notify(config, "Dockpilot: drive detected", f"{info['drive_name']} ({uuid}): {len(info['jobs'])} approved job(s)")
            for job in info["jobs"]:
                job_id = hashlib.sha256(f"{uuid}:{job['id']}:{job['target']}".encode()).hexdigest()[:24]
                receipts = base / "receipts"
                queued = base / "queue"
                staging = base / "staging"
                for directory in (receipts, queued, staging):
                    directory.mkdir(parents=True, exist_ok=True)
                receipt = receipts / f"{job_id}.json"
                queue_file = queued / f"{job_id}.json"
                if receipt.exists():
                    try:
                        if json.loads(receipt.read_text()).get("status") == "success":
                            print(f"Already imported: {job['id']} (receipt {job_id}); skipping")
                            continue
                    except (json.JSONDecodeError, OSError):
                        pass
                if queue_file.exists():
                    print(f"Import already queued: {job['id']}; skipping")
                    continue
                incoming = staging / (job_id + ".partial")
                final = staging / job_id
                if final.exists():
                    raise ValidationError(f"Staging path already exists without queue: {final}; inspect before retrying")

                source = drive / job["source"]
                incoming.mkdir(parents=True, exist_ok=True)
                check_storage(base, job["bytes"])
                notify(config, "Dockpilot: import started", f"{job['id']}: staging {job['media_files']:,} media files; upload will follow")
                print(f"Staging {source} -> {incoming}", flush=True)
                run(["rsync", "-rlt", "--no-owner", "--no-group", "--partial",
                     "--", str(source) + "/", str(incoming) + "/"])
                # Verify full archive (not only extension-filtered media) before queuing.
                verification = run(
                    ["rsync", "-rltcn", "--dry-run", "--out-format=%n", "--",
                     str(source) + "/", str(incoming) + "/"],
                    capture_output=True, text=True,
                )
                if verification.stdout.strip():
                    raise ValidationError(f"Copy verification found differences; staging retained at {incoming}")
                incoming.rename(final)

                record = {
                    "version": 1,
                    "id": job_id,
                    "job": job["id"],
                    "target": job["target"],
                    "action": job["action"],
                    "staging_id": job_id,
                    "media_files": job["media_files"],
                    "created_at": int(time.time()),
                }
                temp = queued / (job_id + ".tmp")
                temp.write_text(json.dumps(record))
                temp.replace(queue_file)
                notify(config, "Dockpilot: upload queued", f"{job['id']}: staged and verified. Immich VM will upload automatically")
                print(f"Queued Immich import {job_id}")
        finally:
            if mounted_here:
                try:
                    run(["umount", str(drive)])
                except subprocess.CalledProcessError:
                    print(f"Warning: could not unmount {drive}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dockpilot trusted USB hotplug job")
    parser.add_argument("--device", required=True)
    parser.add_argument("--config", default=str(HOST_CONFIG))
    args = parser.parse_args(argv)
    try:
        process(args.device, Path(args.config))
        return 0
    except (ValidationError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"Dockpilot hotplug failed: {exc}", file=sys.stderr)
        try:
            notify(read_yaml(Path(args.config)), "Dockpilot: import failed", str(exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
