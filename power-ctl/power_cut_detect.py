"""Power-cut detector on BCM 18 (physical 12).
ON = ~3.0-3.3V -> HIGH, OFF = 0V / floating -> LOW (via pull-down).

All configuration is hardcoded below (CONFIG). No CLI args needed:
  Run: /usr/bin/python3 power_cut_detect.py   (runs forever, for systemd)

Edge actions (Linux target only):
  rising  (OFF -> ON)  : etherwake the PC back up, wait HDD_WAKE_DELAY
                         with GPIO abort + guard, then reattach local HDD.
  falling (ON -> OFF)  : PC shutdown over SSH (with instant GPU pkill
                         chaining) IN PARALLEL with Pi-local HDD spindown
                         (unmount + hdparm standby; stays enumerated and
                         auto-wakes, disk stays plugged).
"""
import os
import re
import subprocess
import threading
import time

from gpiozero import DigitalInputDevice
from gpiozero.pins.lgpio import LGPIOFactory

# ---------------- CONFIG (edit here, no args needed) ----------------
PIN = 18
POLL_SEC = 2
# Fire an edge action only after the new level is stable for this many
# consecutive polls (debounce against relay chatter / brownout flicker).
STABLE_POLLS = 2

MAC = "10:ff:e0:f7:83:7d"          # PC MAC for etherwake
IFACE = "eth0"                     # iface etherwake sends on
SSH_TARGET = "palash@10.10.10.2"   # PC to shut down
# GPU kill first (~100W instant shed in ~5s), then full poweroff (~30s).
# pkill patterns must NOT match this SSH session itself (they don't:
# no 'ssh'/'poweroff' literal in the patterns below).
# BRACKET TRICK (critical): plain -f patterns match the remote shell's OWN
# cmdline (it contains the pattern text), so pkill TERMs its own session
# and sudo poweroff never runs (ssh dies with silent rc=255). [l]lama
# matches "llama" but not the literal "[l]lama" in our own cmdline.
SHUTDOWN_CMD = ("pkill -TERM -f '[l]lama-server'; "
                "pkill -TERM -f '[c]omfy_main.py'; sleep 2; "
                "sudo systemctl poweroff")
SSH_KEY = os.path.expanduser("~/.ssh/powerctl_ed25519")
SSH_RETRIES = 30
SSH_RETRY_DELAY = 10
SSH_CONNECT_TIMEOUT = 10
LINK_WAIT_BUDGET = 30             # seconds to wait for eth0 carrier + ping
                                 # before SSH attempts (own budget, abortable)

HDD_MOUNT = "/mnt/hdd"             # Pi-local mountpoint ("" disables HDD)
HDD_DEVICE = "/dev/disk/by-id/usb-WD_My_Book_25EE_5758373244353635364E3745-0:0"  # whole disk, NOT partition
HDD_PATTERNS = ("[s]yncthing", "[r]sync")  # local writers to stop before unmount
                                 # (bracket trick: never match our own shell)
HDD_USB = "2-2"                    # suffix under /sys/bus/usb/devices
HDD_WAKE_DELAY = 90                # seconds after etherwake before reattach

DRY_RUN = False                    # True = print actions without executing
# --------------------------------------------------------------------


def _exec(cmd, dry_run, timeout=30, capture=False, label=""):
    """Shared subprocess runner. Returns (rc, stderr_text)."""
    prefix = f"{label} " if label else ""
    print(f"$ {' '.join(cmd)}", flush=True)
    if dry_run:
        print("(dry-run, not executed)", flush=True)
        return 0, ""
    try:
        if capture:
            r = subprocess.run(cmd, timeout=timeout,
                               capture_output=True, text=True)
            return r.returncode, (r.stderr or "")
        r = subprocess.run(cmd, timeout=timeout)
        return r.returncode, ""
    except subprocess.TimeoutExpired:
        print(f"{prefix}client hung past {timeout}s (killed)", flush=True)
        return 124, ""
    except Exception as e:  # noqa: BLE001
        print(f"{prefix}action failed: {e}", flush=True)
        return 1, ""


def run(cmd, dry_run, timeout=30):
    rc, _ = _exec(cmd, dry_run, timeout)
    return rc


def run_capture(cmd, dry_run, timeout=30, label=""):
    """run() variant that captures output. Returns (rc, stderr_text)."""
    return _exec(cmd, dry_run, timeout, capture=True, label=label)


def power_back(dev):
    """True if GPIO reads HIGH (mains present). None-safe."""
    return dev is not None and bool(dev.value)


def sleep_abortable(dev, seconds, abort_when=True):
    """Sleep `seconds` in 1s chunks.

    Returns True if aborted early because dev.value == abort_when,
    False if the full wait elapsed. None-safe (never aborts).
    """
    for _ in range(int(seconds)):
        time.sleep(1)
        if dev is not None and bool(dev.value) == bool(abort_when):
            return True
    return False


def ping_host(host, count=2, wait=2, timeout=15, label=""):
    """Single ping probe. Returns True if host answers, False otherwise."""
    prefix = f"{label} " if label else ""
    try:
        r = subprocess.run(["ping", f"-c{count}", f"-W{wait}", host],
                           capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"{prefix}ping error: {e}", flush=True)
        return False


def on_power_on(dry_run=DRY_RUN, dev=None):
    print("POWER ON -> waking target", flush=True)
    run(["sudo", "/usr/sbin/etherwake", "-i", IFACE, MAC], dry_run)
    if dry_run:
        print(f"(dry-run) would: wait {HDD_WAKE_DELAY}s, check GPIO, "
              f"then reattach {HDD_MOUNT}", flush=True)
        reattach_hdd(dry_run)
        return True
    # Settle wait: let mains/PC stabilize before spinning up the HDD.
    # Sleep in 1s chunks so a fresh power-cut aborts promptly.
    print(f"waiting {HDD_WAKE_DELAY}s before HDD wake-up...", flush=True)
    if sleep_abortable(dev, HDD_WAKE_DELAY, abort_when=False):
        print("power cut again during wake delay -> abort HDD wake-up",
              flush=True)
        return False
    # Quick GPIO guard right before touching the disk.
    if dev is not None and not power_back(dev):
        print("GPIO LOW at HDD wake-up check -> abort", flush=True)
        return False
    print("GPIO HIGH confirmed -> waking HDD", flush=True)
    reattach_hdd(dry_run)
    return True


def ssh_shutdown_once(dry_run=DRY_RUN):
    """Single SSH shutdown attempt. Returns returncode (0 = ok).

    ssh's own stderr (refused/timeout/denied) is logged so the next
    failure names itself instead of hiding behind a bare rc=255.
    """
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes",
           "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
           SSH_TARGET, SHUTDOWN_CMD]
    rc, stderr = run_capture(cmd, dry_run,
                             timeout=SSH_CONNECT_TIMEOUT + 20, label="[pc]")
    if dry_run or rc == 0:
        return rc
    if rc == 124:
        # Hang message already logged by run_capture with [pc] prefix.
        return rc
    lines = stderr.strip().splitlines()
    # BatchMode keeps it to ~1 line; log the last (most specific).
    detail = lines[-1].strip() if lines else "(no stderr)"
    print(f"[pc] ssh failed rc={rc}: {detail}", flush=True)
    return rc


def wait_for_pc_link(dry_run=DRY_RUN, dev=None):
    """Gate SSH attempts on carrier + ping. Returns True to proceed.

    After a Pi reboot the service can fire before eth0 has an address
    (unit only orders After=network.target). Fail-open: if the budget
    exhausts we still proceed — the SSH retry loop is the backstop.
    Aborts (False) only if power returns mid-wait.
    """
    pc_host = SSH_TARGET.split("@")[-1]
    print(f"[pc] waiting for link to {pc_host} "
          f"(carrier + ping, {LINK_WAIT_BUDGET}s budget)...", flush=True)
    if dry_run:
        print("[pc] (dry-run) would: check carrier, ping PC", flush=True)
        return True
    deadline = time.monotonic() + LINK_WAIT_BUDGET
    while time.monotonic() < deadline:
        if power_back(dev):
            print("[pc] power returned while waiting for link -> abort",
                  flush=True)
            return False
        try:
            link = subprocess.run(["ip", "link", "show", IFACE],
                                  capture_output=True, text=True, timeout=5)
            carrier = "LOWER_UP" in (link.stdout or "")
        except Exception:
            carrier = False
        if not carrier:
            print(f"[pc] {IFACE} no carrier yet...", flush=True)
            time.sleep(2)
            continue
        try:
            ping = subprocess.run(["ping", "-c1", "-W2", pc_host],
                                  capture_output=True, timeout=8)
            if ping.returncode == 0:
                print("[pc] PC reachable (ping OK), starting SSH", flush=True)
                return True
            print("[pc] carrier up, ping failed, retrying...", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[pc] ping error: {e}", flush=True)
        time.sleep(2)
    print("[pc] link budget exhausted, trying SSH anyway (backstop)",
          flush=True)
    return True


def stop_local_writers(dry_run=DRY_RUN):
    """SIGTERM writers, wait up to 10s, SIGKILL survivors. Best-effort."""
    live = [p for p in HDD_PATTERNS if p.strip()]
    if not live:
        return
    for p in live:
        print(f"stopping writers matching: {p}", flush=True)
        run(["pkill", "-TERM", "-f", p], dry_run)
    if dry_run:
        return
    for _ in range(20):
        alive = any(
            subprocess.run(["pgrep", "-f", p],
                           capture_output=True).returncode == 0
            for p in live
        )
        if not alive:
            break
        time.sleep(0.5)
    for p in live:
        run(["pkill", "-KILL", "-f", p], dry_run)


def spindown_hdd(dry_run=DRY_RUN):
    """Unmount + standby the Pi-attached HDD. Stays enumerated (auto-wake).

    NOTE: udisksctl power-off DETACHES this WD My Book beyond remote
    recovery (USB re-enumeration fails, needs physical replug), so we
    deliberately do NOT use it. hdparm -y sends ATA STANDBY: heads park,
    platters stop, device stays visible and auto-wakes on next access.
    """
    print(f"[hdd] SPINDOWN -> mount={HDD_MOUNT}", flush=True)
    stop_local_writers(dry_run)
    run(["sync"], dry_run, timeout=60)
    if dry_run:
        print(f"(dry-run) would: umount {HDD_MOUNT} if mounted, "
              f"hdparm -y standby", flush=True)
        return True
    try:
        # Unmount stacked mounts too (mount -a over an existing mount
        # stacks a second instance; one umount is not enough).
        for i in range(3):
            r = subprocess.run(["findmnt", HDD_MOUNT], capture_output=True,
                               timeout=10)
            if r.returncode != 0:
                print(f"[hdd] {HDD_MOUNT} not mounted, skip unmount",
                      flush=True)
                break
            print(f"[hdd] unmounting {HDD_MOUNT} (pass {i + 1})", flush=True)
            # NOTE: fstab mounts are NOT managed by udisks (udisksctl
            # unmount errors on them), so try sudo umount first.
            rc = run(["sudo", "umount", HDD_MOUNT], False, timeout=60)
            if rc != 0:
                # Diagnose the holder so the journal names it (cwd shells,
                # daemons, Kit components). fuser is read-only.
                try:
                    fz = subprocess.run(["fuser", "-vm", HDD_MOUNT],
                                        capture_output=True, text=True,
                                        timeout=10)
                    out = ((fz.stdout or "") + (fz.stderr or "")).strip()
                    print(f"[hdd] umount busy holders:\n{out}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[hdd] fuser probe failed: {e}", flush=True)
                # -b takes a block device, not a mountpoint: resolve the
                # first partition of the whole disk (sda -> sda1). Under
                # sudo (root bypasses the polkit agent headless SSH lacks).
                base = os.path.realpath(HDD_DEVICE)
                part = base + "1" if re.match(r"^/dev/sd[a-z]+$", base) \
                    else base + "-part1"
                print(f"[hdd] trying sudo udisksctl unmount {part}",
                      flush=True)
                run(["sudo", "udisksctl", "unmount", "-b", part],
                    False, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"[hdd] unmount check failed: {e}", flush=True)
    # udisksctl chokes on /dev/disk/by-id paths (colons break its object
    # lookup), so resolve to the real /dev/sdX node first.
    dev_node = os.path.realpath(HDD_DEVICE)
    print(f"[hdd] standby {dev_node} (from {HDD_DEVICE})", flush=True)
    rc = run(["sudo", "hdparm", "-y", dev_node], False, timeout=60)
    if rc == 0:
        print("[hdd] standby OK (parked, stays enumerated, auto-wakes)",
              flush=True)
        return True
    print(f"[hdd] hdparm standby rc={rc}", flush=True)
    return False


def reattach_hdd(dry_run=DRY_RUN):
    """Mount after power return. Device stays enumerated (standby auto-wakes
    on access), so this is normally just a mount. USB re-authorize kept as
    fallback for the case the node actually vanished."""
    print(f"[hdd] REATTACH -> mount {HDD_MOUNT}", flush=True)
    if dry_run:
        print("(dry-run) would: mount (device stays enumerated)", flush=True)
        return True
    dev_node = os.path.realpath(HDD_DEVICE)
    if not os.path.exists(dev_node):
        auth = f"/sys/bus/usb/devices/{HDD_USB}/authorized"
        print(f"[hdd] {dev_node} missing, trying USB re-authorize {auth}",
              flush=True)
        try:
            with open(auth, "w") as f:
                f.write("1\n")
            print("[hdd] USB re-authorized", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[hdd] USB re-authorize failed: {e}", flush=True)
        time.sleep(10)
    for _ in range(30):
        r = subprocess.run(["findmnt", HDD_MOUNT], capture_output=True,
                           timeout=10)
        if r.returncode == 0:
            print(f"{HDD_MOUNT} already mounted", flush=True)
            return True
        time.sleep(2)
    # fstab is fixed, so plain mount-by-mountpoint is the reliable path;
    # udisks mount needs the partition node, not the mountpoint.
    rc = run(["sudo", "mount", HDD_MOUNT], False, timeout=60)
    if rc != 0:
        part = os.path.realpath(HDD_DEVICE) + "1"
        print(f"[hdd] fstab mount failed, trying udisksctl mount {part}",
              flush=True)
        run(["udisksctl", "mount", "-b", part], False, timeout=60)
    return True


def on_power_off(dry_run=DRY_RUN, dev=None):
    """PC shutdown + Pi-HDD spindown IN PARALLEL. Returns PC-shutdown ok.

    The two jobs touch different machines (PC over SSH vs Pi-local disk)
    so there is no ordering dependency; running them together cuts total
    exposure from ~60s to ~30s. HDD park still runs even if PC SSH fails.
    """
    print("POWER OFF (cut) -> PC shutdown + HDD spindown (parallel)",
          flush=True)
    hdd_result = {}

    def _hdd_job():
        if power_back(dev):
            print("[hdd] power already back -> skip spindown", flush=True)
            hdd_result["ok"] = False
            return
        if not HDD_MOUNT:
            print("[hdd] spindown skipped (HDD_MOUNT empty)", flush=True)
            hdd_result["ok"] = False
            return
        hdd_result["ok"] = spindown_hdd(dry_run)

    hdd_thread = threading.Thread(target=_hdd_job, daemon=True)
    hdd_thread.start()
    pc_ok = False
    attempts = 0
    if dry_run:
        ssh_shutdown_once(dry_run=True)
        pc_ok = True
    else:
        if not wait_for_pc_link(dry_run=False, dev=dev):
            print("[pc] link wait aborted (power back), skipping SSH",
                  flush=True)
        else:
            for attempt in range(1, SSH_RETRIES + 2):
                if power_back(dev):
                    print("[pc] power returned during shutdown retries -> abort",
                          flush=True)
                    break
                rc = ssh_shutdown_once(dry_run=False)
                attempts = attempt
                if rc == 0:
                    print(f"[pc] shutdown delivered (attempt {attempt})",
                          flush=True)
                    pc_ok = True
                    break
                if attempt > SSH_RETRIES:
                    break
                print(f"[pc] retry {attempt}/{SSH_RETRIES} in {SSH_RETRY_DELAY}s",
                      flush=True)
                if sleep_abortable(dev, SSH_RETRY_DELAY, abort_when=True):
                    print("[pc] power returned during shutdown retries "
                          "-> abort", flush=True)
                    pc_ok = False
                    break
        if not pc_ok:
            print(f"[pc] shutdown FAILED after {attempts} attempt(s) "
                  f"(HDD job continues independently)", flush=True)
    # Wait for the HDD job (cap so a wedged unmount can't hang us).
    hdd_thread.join(timeout=60)
    if hdd_thread.is_alive():
        print("[hdd] spindown still running after 60s cap (leaving it)",
              flush=True)
    return pc_ok


BOOT_OFF_SETTLE = 60              # seconds to wait after boot-into-OFF
                                 # before probe-and-act (abort if power returns)
BOOT_ON_SETTLE = 60              # seconds to wait after boot-into-ON
                                 # (network/NTP/disk enumeration; abort on cut)


def boot_on_reconcile(dev, dry_run=DRY_RUN):
    """Handle service start while power is already ON.

    No edge will fire for an already-ON world, so two passive recoveries
    would never run: (1) PC on BIOS Stay-Off needs an explicit etherwake;
    (2) fstab may have missed a slowly-enumerating HDD. Settle, then check
    both and act once. Every step is logged. Returns True if all fine.
    """
    print("[boot-on] started with power already ON (no edge will fire)",
          flush=True)
    print(f"[boot-on] settling {BOOT_ON_SETTLE}s for network/disk "
          f"(abort if power cuts)...", flush=True)
    if sleep_abortable(dev, BOOT_ON_SETTLE, abort_when=False):
        print("[boot-on] power cut during settle -> normal monitoring",
              flush=True)
        return False
    pc_host = SSH_TARGET.split("@")[-1]
    pc_up = ping_host(pc_host, count=2, wait=2, label="[boot-on]")
    if pc_up:
        print("[boot-on] PC already up -> no etherwake needed", flush=True)
    else:
        print("[boot-on] PC silent (BIOS Stay-Off?) -> one etherwake",
              flush=True)
        run(["sudo", "/usr/sbin/etherwake", "-i", IFACE, MAC], dry_run)
    reattach_hdd(dry_run)
    print("[boot-on] reconcile done, monitoring", flush=True)
    return True
PC_PING_TIMEOUT = 2              # per-ping wait for the boot probe


def boot_probe_and_act(dev, dry_run=DRY_RUN):
    """Handle service start while power is already OFF (outage in progress).

    The normal gate stays silent here to avoid shutdown-on-maintenance-boot.
    But on battery every minute of PC uptime counts, so: settle, re-check
    power, ping the PC — if it answers while mains is still out, run the
    standard shutdown path once. Every step is logged.
    Returns True if the shutdown path ran, False otherwise.
    """
    print("[boot] started during power cut (GPIO LOW at boot)", flush=True)
    print(f"[boot] settling {BOOT_OFF_SETTLE}s for network/NTP/carrier "
          f"(abort if power returns)...", flush=True)
    if sleep_abortable(dev, BOOT_OFF_SETTLE, abort_when=True):
        print("[boot] power returned during settle -> normal monitoring",
              flush=True)
        return False
    if power_back(dev):
        print("[boot] power back at settle end -> normal monitoring",
              flush=True)
        return False
    print("[boot] power still OUT, probing PC...", flush=True)
    pc_host = SSH_TARGET.split("@")[-1]
    pc_up = ping_host(pc_host, count=2, wait=PC_PING_TIMEOUT, label="[boot]")
    if not pc_up:
        print("[boot] PC not answering (off/unreachable) -> nothing to do, "
              "monitoring", flush=True)
        return False
    print("[boot] PC IS UP on battery -> running shutdown path once",
          flush=True)
    on_power_off(dry_run=dry_run, dev=dev)
    print("[boot] boot-time shutdown path done, monitoring", flush=True)
    return True


def main():
    dev = DigitalInputDevice(PIN, pull_up=False, pin_factory=LGPIOFactory())
    print(f"Monitoring BCM {PIN} forever (pull-down).", flush=True)
    last = dev.value
    print("POWER ON" if last else "POWER OFF (cut)", flush=True)
    target_up = bool(last)
    if last:
        # Booted into mains-ON: no edge will ever fire for this state, so
        # reconcile once (PC wake if BIOS Stay-Off kept it down, HDD mount
        # if fstab missed a slow disk), then monitor normally.
        boot_on_reconcile(dev)
        last = dev.value
        print(f"POWER {'ON' if last else 'OFF (cut)'} (post-reconcile)",
              flush=True)
        target_up = bool(last)
        if not last:
            # Power died during reconcile: fall through to probe-and-act.
            boot_probe_and_act(dev)
            target_up = False
            last = dev.value
    else:
        # Outage already in progress at service start: probe-and-act
        # (one-shot), then continue with the gate consumed.
        boot_probe_and_act(dev)
        target_up = False
        last = dev.value
        print(f"POWER {'ON' if last else 'OFF (cut)'} (post-probe state)",
              flush=True)
        if last:
            target_up = True
    stable = 0
    try:
        while True:
            v = dev.value
            if v != last:
                stable += 1
                if stable >= STABLE_POLLS:
                    if v:
                        on_power_on(dev=dev)
                        target_up = True
                    else:
                        if not target_up:
                            print("POWER OFF (cut) (target not enabled, "
                                  "shutdown skipped)", flush=True)
                        else:
                            on_power_off(dev=dev)
                        target_up = False
                    last = v
                    stable = 0
            else:
                stable = 0
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
