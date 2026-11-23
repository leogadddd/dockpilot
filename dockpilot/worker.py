"""VM-side import worker. No Immich API key ever enters the Proxmox host."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .cli import ValidationError, notify_ntfy, read_yaml

VALID_ID = re.compile(r"^[0-9a-f]{24}$")


def notify(config: dict, title: str, message: str) -> None:
    try:
        notify_ntfy(config, title, message)
    except Exception as exc:
        print(f"Notification warning: {exc}", file=sys.stderr)


def write_receipt(path: Path, payload: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def run_once(config_path: Path) -> int:
    config = read_yaml(config_path)
    settings = config.get("worker", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        raise ValidationError("VM worker is not enabled")
    root = Path(settings.get("shared_root", "/data/dockpilot")).resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("Missing shared directory")
    queue_dir, receipts = root / "queue", root / "receipts"
    queue_dir.mkdir(exist_ok=True)
    receipts.mkdir(exist_ok=True)

    # Prevent overlapping timer invocations.
    with open("/run/lock/dockpilot-worker.lock", "w") as lock:
        if not fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB) is None:
            pass
        failures = 0
        for item in sorted(queue_dir.glob("*.json")):
            if item.is_symlink():
                continue
            try:
                record = json.loads(item.read_text())
                task_id = record.get("id")
                if (record.get("version") != 1 or
                    not isinstance(task_id, str) or
                    not VALID_ID.fullmatch(task_id) or
                    item.name != task_id + ".json" or
                    record.get("staging_id") != task_id or
                    record.get("action") != "immich.import"):
                    raise ValidationError("Invalid queued job")
                target_name = record.get("target")
                targets = config.get("targets", {})
                target = targets.get(target_name) if isinstance(targets, dict) else None
                if (not isinstance(target, dict) or target.get("type") != "immich" or
                    "immich.import" not in target.get("allowed_actions", [])):
                    raise ValidationError("Unapproved worker target")
                staging = root / "staging" / task_id
                if staging.is_symlink() or not staging.is_dir() or staging.resolve() != staging:
                    raise ValidationError("Unsafe or missing staged archive")
                env_file = Path(settings.get("immich_env_file", "/etc/dockpilot/immich.env"))
                if env_file.is_symlink() or not env_file.is_file():
                    raise ValidationError("Missing Immich credentials file")
                if env_file.stat().st_mode & 0o077:
                    raise ValidationError("Immich credentials file must be mode 0600")
                receipt = receipts / (task_id + ".json")
                if receipt.exists() and json.loads(receipt.read_text()).get("status") == "success":
                    item.unlink()
                    continue
                notify(config, "Dockpilot: Immich upload started",
                       f"{record.get('job')}: uploading {record.get('media_files')} files to {target_name}")
                print(f"Importing {task_id} to {target_name}", flush=True)
                command = [
                    "docker", "run", "--rm", "--network", "host",
                    "--env-file", str(env_file),
                    "--mount", f"type=bind,source={staging},target=/import,readonly",
                    settings.get("cli_image", "ghcr.io/immich-app/immich-cli:latest"),
                    "upload", "--recursive", "/import",
                ]
                # Default hash checking enables the CLI's duplicate checks.
                result = subprocess.run(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                print(result.stdout, flush=True)
                if result.returncode:
                    raise ValidationError(f"Immich CLI exited {result.returncode}; see worker journal for details")
                write_receipt(receipt, {
                    "version": 1, "id": task_id, "status": "success",
                    "finished_at": int(time.time()), "job": record.get("job"),
                    "target": target_name,
                    "note": "Immich CLI completed; file-count and server processing should be checked",
                })
                item.unlink()
                notify(config, "Dockpilot: Immich import completed",
                       f"{record.get('job')}: CLI finished successfully. Check Immich for indexing and duplicates")
            except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                failures += 1
                print(f"Worker failed on {item.name}: {exc}", file=sys.stderr)
                notify(config, "Dockpilot: Immich import failed", f"{item.name}: {exc}")
                # Leave failed job in queue, but do not retry forever every minute.
                failed = queue_dir / (item.stem + ".failed")
                item.replace(failed)
        return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run queued Immich imports in media VM")
    parser.add_argument("--config", default="/etc/dockpilot/config.yaml")
    args = parser.parse_args(argv)
    try:
        return run_once(Path(args.config))
    except (ValidationError, OSError, ValueError) as exc:
        print(f"Dockpilot worker failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
