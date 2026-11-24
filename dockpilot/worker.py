"""VM-side import worker. No Immich API key ever enters the Proxmox host."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from .cli import ValidationError, notify_ntfy, read_yaml

VALID_ID = re.compile(r"^[0-9a-f]{24}$")
LOCK_PATH = "/run/lock/dockpilot-worker.lock"


class UploadError(Exception):
    """An uploader or Docker error that can be retried."""


class ImportInterrupted(Exception):
    """The worker was asked to stop."""


def notify(config: dict, title: str, message: str) -> None:
    try:
        notify_ntfy(config, title, message)
    except Exception as exc:
        print(f"Notification warning: {exc}", file=sys.stderr)


def write_json(path: Path, payload: dict):
    tmp = path.with_suffix(".worker-tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def worker_config(config_path: Path):
    config = read_yaml(config_path)
    settings = config.get("worker", {})
    if not isinstance(settings, dict) or not settings.get("enabled", False):
        raise ValidationError("VM worker is not enabled")
    root = Path(settings.get("shared_root", "/data/dockpilot")).resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("Missing shared directory")
    return config, settings, root


@contextmanager
def worker_lock():
    with open(LOCK_PATH, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationError("Another Dockpilot worker is active") from exc
        yield


def run_docker(command, stop_requested, *, quiet=False):
    if stop_requested():
        raise ImportInterrupted
    with subprocess.Popen(command, stdout=subprocess.DEVNULL if quiet else None) as process:
        while True:
            try:
                status = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                if stop_requested():
                    process.kill()
                    raise ImportInterrupted
                continue
            if status == 0:
                return
            if stop_requested():
                raise ImportInterrupted
            raise UploadError(f"Docker {command[1]} exited {status}; see worker journal")


def clean_container(task_id):
    # Match only containers created for this Dockpilot job.
    find = ["docker", "ps", "-aq", "--filter", f"label=org.dockpilot.import={task_id}"]
    containers = subprocess.check_output(find, text=True, timeout=5).split()
    if not containers:
        return
    try:
        subprocess.run(["docker", "stop", "-t", "10", *containers], timeout=20)
    except subprocess.TimeoutExpired:
        pass
    subprocess.run(["docker", "rm", "-f", *containers], check=True, timeout=10)


def upload(settings, task_id, staging, env_file, stop_requested):
    # Separate creation and start so a stop during an image pull cannot start an upload.
    # Keep removal under worker control to avoid racing Docker's automatic removal.
    name = f"dockpilot-import-{task_id}"
    command = [
        "docker", "create", "--init", "--network", "host", "--name", name,
        "--label", f"org.dockpilot.import={task_id}",
        "--env-file", str(env_file),
        "--mount", f"type=bind,source={staging},target=/import,readonly",
        settings.get("cli_image", "ghcr.io/immich-app/immich-cli:latest"),
        "upload", "--recursive", "/import",
    ]
    try:
        clean_container(task_id)
        try:
            run_docker(command, stop_requested, quiet=True)
            # Attach directly to the journal, preserving the Immich CLI exit status.
            run_docker(["docker", "start", "-a", name], stop_requested)
        finally:
            clean_container(task_id)
    except (subprocess.SubprocessError, OSError) as exc:
        if stop_requested():
            raise ValidationError(f"Could not confirm stopped container {name}: {exc}") from exc
        raise UploadError(f"Docker error for {name}: {exc}") from exc


def retry_failed(config_path: Path, task_id: str) -> int:
    if not VALID_ID.fullmatch(task_id):
        raise ValidationError("Expected a 24-character Dockpilot job ID")
    _, _, root = worker_config(config_path)
    item = root / "queue" / f"{task_id}.json"
    failed = item.with_suffix(".failed")
    with worker_lock():
        if item.exists() or item.is_symlink():
            raise ValidationError("Job is already queued")
        if failed.is_symlink() or not failed.is_file():
            raise ValidationError("Failed job does not exist")
        record = json.loads(failed.read_text())
        if not isinstance(record, dict):
            raise ValidationError("Invalid failed job")
        record.pop("worker_retry", None)
        write_json(failed, record)
        failed.replace(item)
    print(f"Queued {task_id} for retry", flush=True)
    return 0


def run_once(config_path: Path, stop_requested=lambda: False) -> int:
    config, settings, root = worker_config(config_path)
    max_retries = settings.get("max_retries", 3)
    delay = settings.get("retry_delay_seconds", 120)
    if type(max_retries) is not int or max_retries < 0:
        raise ValidationError("max_retries must be a nonnegative integer")
    if type(delay) is not int or delay < 1:
        raise ValidationError("retry_delay_seconds must be a positive integer")
    queue_dir, receipts = root / "queue", root / "receipts"
    queue_dir.mkdir(exist_ok=True)
    receipts.mkdir(exist_ok=True)

    with worker_lock():
        failures = 0
        for item in sorted(queue_dir.glob("*.json")):
            if stop_requested():
                break
            if item.is_symlink():
                continue
            try:
                record = json.loads(item.read_text())
                if not isinstance(record, dict):
                    raise ValidationError("Invalid queued job")
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
                receipt = receipts / (task_id + ".json")
                if receipt.exists():
                    saved = json.loads(receipt.read_text())
                    if isinstance(saved, dict) and saved.get("status") == "success":
                        item.unlink()
                        continue
                staging = root / "staging" / task_id
                if staging.is_symlink() or not staging.is_dir() or staging.resolve() != staging:
                    raise ValidationError("Unsafe or missing staged archive")
                env_file = Path(settings.get("immich_env_file", "/etc/dockpilot/immich.env"))
                if env_file.is_symlink() or not env_file.is_file():
                    raise ValidationError("Missing Immich credentials file")
                if env_file.stat().st_mode & 0o077:
                    raise ValidationError("Immich credentials file must be mode 0600")
                retry = record.get("worker_retry", {})
                if (not isinstance(retry, dict) or
                    type(retry.get("failures", 0)) is not int or retry.get("failures", 0) < 0 or
                    type(retry.get("not_before", 0)) is not int):
                    raise ValidationError("Invalid retry state")
                if time.time() < retry.get("not_before", 0):
                    continue
                notify(config, "Dockpilot: Immich upload started",
                       f"{record.get('job')}: uploading {record.get('media_files')} files to {target_name}")
                print(f"Importing {task_id} to {target_name}", flush=True)
                # Default hash checking enables the CLI's duplicate checks.
                upload(settings, task_id, staging, env_file, stop_requested)
                write_json(receipt, {
                    "version": 1, "id": task_id, "status": "success",
                    "finished_at": int(time.time()), "job": record.get("job"),
                    "target": target_name,
                    "note": "Immich CLI completed; file-count and server processing should be checked",
                })
                item.unlink()
                notify(config, "Dockpilot: Immich import completed",
                       f"{record.get('job')}: CLI finished successfully. Check Immich for indexing and duplicates")
            except ImportInterrupted:
                print(f"Stopped import {item.stem}; job remains queued", flush=True)
                break
            except UploadError as exc:
                if stop_requested():
                    break
                attempts = retry.get("failures", 0) + 1
                if attempts <= max_retries:
                    wait = delay * 2 ** min(attempts - 1, 3)
                    record["worker_retry"] = {"failures": attempts, "not_before": int(time.time()) + wait}
                    write_json(item, record)
                    print(f"Import {item.stem}: {exc}. Retry {attempts}/{max_retries} in {wait}s",
                          file=sys.stderr, flush=True)
                    continue
                failures += 1
                fail_job(config, item, f"{exc} (retry limit reached)")
            except (ValueError, ValidationError) as exc:
                if stop_requested():
                    # Leave the job queued even if Docker failed to confirm cleanup.
                    raise
                failures += 1
                fail_job(config, item, exc)
        return 1 if failures else 0


def fail_job(config, item, reason):
    item.replace(item.with_suffix(".failed"))
    print(f"Worker failed on {item.name}: {reason}", file=sys.stderr, flush=True)
    notify(config, "Dockpilot: Immich import failed", f"{item.name}: {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run queued Immich imports in media VM")
    parser.add_argument("--config", default="/etc/dockpilot/config.yaml")
    parser.add_argument("--retry", metavar="JOB_ID", help="requeue an exhausted or legacy failed job")
    args = parser.parse_args(argv)
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.retry:
            return retry_failed(Path(args.config), args.retry)
        return run_once(Path(args.config), lambda: stopping)
    except (ValidationError, OSError, ValueError) as exc:
        print(f"Dockpilot worker failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
