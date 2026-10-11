# power-ctl — operating inventory

Everything `power_cut_detect.py` on `blr-pi` needs around it to work properly.
Verified live 2026-10-11 across repeated real wall-cut tests. If any line
below breaks, the noted phase goes silent.

## 1. Hardware

- **Pi (`blr-pi`)** with GPIO 18 wired to the mains-sense circuit (pull-down;
  HIGH = power, LOW = cut). The single sensor of the whole system.
- **Sense-side power:** the GPIO circuit must ride *mains*, not the UPS output —
  else a cut never registers LOW and nothing fires.
- **WD My Book 4TB** on Pi USB (port `2-2`, `sda`), exFAT, expected at `/mnt/hdd`.
  Stays plugged; `hdparm -y` standby (not detach) is the power-saving mechanism.
- **PC (`big-boy`)** on direct Ethernet to Pi (`10.10.10.2`), ATX standby alive
  so its NIC hears WoL.
- **UPS 180W (~8Wh usable, new).** Carries PC + HDD + Pi. A second unit is
  pending — until then the 360W PC is ~2x overload and must die within seconds
  of a cut (it does: chained pkill + poweroff, delivered attempt-1, proven).

## 2. Pi software

- `python3-gpiozero` (edge sensing), `hdparm` (standby), `exfatprogs` (fsck for
  the inevitable dirty bit), OpenSSH client, `etherwake`, `fuser` (busy diagnostics).
- `power_cut_detect.py` v8 — the only GPIO-18 consumer. Nothing else may claim pin 18.
- `power-cut-detect.service`: `enabled`, `After=network-online.target` +
  `Wants=network-online.target` (NOT bare `network.target` — the service must
  not fire before `eth0` has its address), bare `ExecStart` (no CLI args; all
  config is the hardcoded CONFIG block), `Restart=on-failure`.
- Static `10.10.10.1/30` on `eth0` (`direct-bigboy` NM profile). No DHCP
  anywhere on the `10.10.10.x` link — DHCP would vanish mid-outage.

## 3. Pi files & permissions

- `/home/palash/power-ctl/power_cut_detect.py` + `power-cut-detect.service`
  (repo mirror: `~/git/local-ai/power-ctl/`).
- `/etc/fstab`: `UUID=2AD4-5F3B -> /mnt/hdd` exFAT
  (`defaults,uid=1000,gid=1000,nofail,noatime`; backup `/etc/fstab.bak-20261011`).
- `~/.ssh/powerctl_ed25519` (`0600`) + `.pub` — Pi→PC identity.
- `/etc/sudoers.d/powerctl-local` (`0440`, `visudo -c` clean):
  `NOPASSWD: /bin/umount, /bin/mount, /usr/sbin/hdparm, /usr/sbin/etherwake, /usr/bin/udisksctl`.
  Passwordless sudo here is load-bearing, not convenience — a reboot wipes the
  timestamp cache, and the first outage after any reboot would fail on password
  prompts (observed live 2026-10-11).

## 4. PC side (`palash@10.10.10.2`)

- `~/.ssh/authorized_keys` trusts the Pi key.
- `/etc/sudoers.d/powerctl-poweroff` (plus legacy twin `powerctl`, identical
  grant, harmless): `NOPASSWD: /usr/bin/systemctl poweroff`. Command-specific
  by design — broader probes (`sudo -n true`) correctly demand a password.
- `/usr/bin/systemctl` + `/usr/bin/pkill` at rule-matching paths.
- **BIOS: Stay Off on AC loss** — no self-boot inrush onto a weak UPS.
  (Power-On / Last-State both re-trip a half-charged 180W pack; observed live.)
- Sheddable load: `llama-server` / `comfy_main.py`, killed via bracket patterns
  (`'[l]lama-server'`) — plain `-f` patterns match the remote shell's OWN
  cmdline and suicide the session (silent rc=255; observed + proven live).

## 5. Behavior contract (edge → action map)

- Falling edge (HIGH→LOW, debounced 2 polls): link gate (carrier + ping, 30s)
  → chained SSH shutdown + parallel HDD `pkill → sync → umount×3 → hdparm -y`.
- Rising edge: `etherwake` → 90s settle (abort on fresh cut) → GPIO guard →
  HDD mount-check.
- Boot into OFF: 60s settle → ping PC → full shutdown path once if PC answers.
- Boot into ON: 60s settle → one `etherwake` if PC silent → HDD mount-check.
- Standby is wake-on-access: ANY touch of `/mnt/hdd` (even checking whether it
  sleeps) re-spins it. Verify with `findmnt`, never `ls`, during an outage.

## 6. Operational truths

- Pi clock is fake-hwclock + NTP: early-boot log timestamps lie until sync.
- `udisksctl power-off` is BANNED for this WD enclosure: it detaches beyond
  remote recovery (kernel port power-cycle + USB authorize both fail with
  "unable to enumerate"; needs physical replug — observed live).
- exFAT will come back dirty after any unclean cut (`fsck.exfat` to clear;
  data presence, not bit cleanliness, is the pass criterion).
- Backups: 12K config tarball on the HDD (contents verified); full SD image
  open (rerun with `set -o pipefail` + size gate — a bare pipe false-passed
  20 empty bytes once); this repo copy checksummed against the live file.

## 7. Failure → symptom map

| Broken line            | Silent phase                              |
|------------------------|-------------------------------------------|
| Sense wire / GPIO      | nothing ever fires                        |
| Pi→PC key              | instant rc=255, PC stays up                |
| PC sudoers             | password prompt mid-outage, PC stays up    |
| Pi sudoers             | umount/hdparm die, disk never parks        |
| fstab UUID             | disk never mounts (reattach no-ops)        |
| BIOS Power-On          | UPS trips on restore inrush                |
| Plain pkill patterns   | SSH session suicides (silent rc=255)       |
| DHCP on 10.10.10.x     | link gate + SSH fail after any reboot      |
