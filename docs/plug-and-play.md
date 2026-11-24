# Plug-and-play deployment (two hosts)

This is an **experimental** implementation. Test only with copies or non-critical drive data first. The original USB filesystem is mounted **read-only** and Dockpilot does not delete it.

## Design

- **Proxmox host:** udev triggers a systemd unit; the host checks a configured trusted UUID, mounts the drive read-only, validates `homelab.yaml`, stages and checksum-verifies the complete source archive on the persistent HDD, then writes an atomic JSON queue entry.
- **Debian media VM:** a systemd timer reads queued jobs from the VirtioFS share. It starts the official Immich CLI Docker container with the staged folder mounted read-only and credentials from a root-owned environment file **on the VM**. Success leaves an audit receipt. An interrupted import stays queued, and upload failures retry with backoff.
- **Notifications:** ntfy sends drive detected, staging started, upload queued, upload started, upload completed, and failures. Notifications are best effort; delivery requires configuring authentication/environment on **both** hosts.

## 1. On Proxmox host

```bash
apt install -y git python3-yaml exfatprogs rsync util-linux
git clone https://github.com/leogadddd/dockpilot.git /opt/dockpilot
mkdir -p /etc/dockpilot /mnt/pve/media-hdd/dockpilot
cp /opt/dockpilot/config/host.example.yaml /etc/dockpilot/config.yaml
nano /etc/dockpilot/config.yaml
```

Set `trusted_uuids: ["D8CC-49FF"]` for the initial known SanDisk, and replace the ntfy URL. Other drives can be registered later; manifests never override the host UUID allowlist. Ensure your `homelab.yaml` uses `target: immich-main` and `mode: copy`. Do **not** include secrets in removable drive manifests.

```bash
cp /opt/dockpilot/systemd/dockpilot-hotplug@.service /etc/systemd/system/
cp /opt/dockpilot/systemd/90-dockpilot.rules /etc/udev/rules.d/
systemctl daemon-reload
udevadm control --reload-rules
```

This only affects **future insertion events**, not an already mounted disk. Prior to unplugging a previously manually mounted drive, run `umount /mnt/homelab-usb` and make sure no process is using the drive. The automatic worker uses a different mount point under `/mnt/dockpilot-drives`.

Monitor host logs with `journalctl -fu dockpilot-hotplug@sdd1.service` (the kernel name may change; list with `systemctl list-units 'dockpilot-hotplug*'`).

## 2. Ensure VirtioFS visibility

The HDD is already shared to the Debian VM as `/data`. The queue root must correspond exactly to `/mnt/pve/media-hdd/dockpilot` on Proxmox and `/data/dockpilot` inside the VM.

Verify `ls -ld /data/dockpilot` inside the VM. Never point the agent at Immich's managed library folder.

## 3. On Debian VM

```bash
sudo apt install -y git python3-yaml docker.io
sudo git clone https://github.com/leogadddd/dockpilot.git /opt/dockpilot
sudo mkdir -p /etc/dockpilot
sudo cp /opt/dockpilot/config/worker.example.yaml /etc/dockpilot/config.yaml
sudo nano /etc/dockpilot/config.yaml
```

If Docker is already installed, do not reinstall it; `docker.io` above is only for new setups.

Create credentials securely, using `sudo nano /etc/dockpilot/immich.env`:

```dotenv
IMMICH_INSTANCE_URL=http://127.0.0.1:2283/api
IMMICH_API_KEY=PASTE_YOUR_IMMICH_API_KEY_HERE
```

The URL is accessed from a Docker container using host networking on Linux. Use the API endpoint `/api`, as required by the Immich CLI. The API key controls which Immich account owns uploaded assets. Never place the key on the Proxmox host or portable drive.

```bash
sudo chmod 600 /etc/dockpilot/immich.env
sudo cp /opt/dockpilot/systemd/dockpilot-worker.service /etc/systemd/system/
sudo cp /opt/dockpilot/systemd/dockpilot-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dockpilot-worker.timer
```

Check `systemctl list-timers dockpilot-worker.timer` and `journalctl -u dockpilot-worker.service -n 50 --no-pager`.

For an existing VM, stop both units before updating the checkout and copying the new service file, then run `sudo systemctl daemon-reload`. The existing worker config can be kept. If a container from the old worker was running, verify it has exited before starting the updated worker.

## 4. Run a full insertion test

1. Check disk capacity. Staging plus uploaded assets requires roughly twice the archive size and spare room for thumbnails.
2. Confirm the manual mount is unmounted. Only unplug with the drive quiescent.
3. Plug the configured SanDisk in again.
4. Check `systemctl list-units 'dockpilot-hotplug*'`, `journalctl -u 'dockpilot-hotplug@*' -f`, `find /mnt/pve/media-hdd/dockpilot -maxdepth 2 -type f`.
5. Inside the VM, follow `journalctl -fu dockpilot-worker.service`.
6. Expect ntfy start/queued/completed/failure notifications. A successful CLI exit does not confirm that Immich's thumbnail jobs have completed.

## Stop, resume, and retry

Run these commands on the Debian media VM. To stop imports and keep them stopped across a reboot:

```bash
sudo systemctl disable --now dockpilot-worker.timer
sudo systemctl stop dockpilot-worker.service
```

The worker stops its upload container and leaves the staged archive and queue record in place. To resume:

```bash
sudo systemctl enable --now dockpilot-worker.timer
sudo systemctl start --no-block dockpilot-worker.service
sudo journalctl -fu dockpilot-worker.service
```

The CLI scans the staged archive again and uses Immich's duplicate checks for assets already uploaded. An interrupted file can be uploaded again. If the timer stays enabled, it picks up unfinished jobs after a reboot.

By default, Docker and upload failures are retried up to three times, with delays of 2, 4, and 8 minutes. The next timer tick starts each retry after its delay. Set `worker.max_retries` to `0` to disable automatic retries, and set `worker.retry_delay_seconds` to change the initial delay. Invalid jobs and exhausted retries are saved as `.failed`. After reviewing the cause in the journal, requeue a failed job by its ID:

```bash
cd /opt/dockpilot
sudo python3 -m dockpilot.worker --retry JOB_ID
```

The next timer tick runs that job when the timer is enabled.

## Receipts and security

- After a **successful** import, the receipt prevents a second upload for the same UUID/job/target. The same drive will therefore not re-import later edits until you explicitly create a new job ID or clear the receipt.
- Keep the USB and staged copies until you have verified your assets appear in Immich and confirmed independent backups.
- VM worker requires privileged Docker access; only system administrators may modify the queue, configs or staging directories. No user-supplied shell commands are executed.
- `ntfy` uses `NTFY_USERNAME` and `NTFY_PASSWORD` in the service environment if login is required. Use a dedicated write-only publisher account in production.
- **Important:** The host configuration is trusted; the drive configuration is not. Never approve untrusted removable media or let untrusted users write to the queue directory.
