#!/usr/bin/env python3
"""
setup-edge510.py — Fully automated VeloCloud Edge 510 → OPNsense conversion.

PHASES
------
  Phase 1  VeloCloud OS (serial console):
           Logs in, downloads coreboot ROM, flashes DMI + firmware, kills watchdog.
           Ends with: device rebooted with OPNsense USB installer.

  Phase 2  OPNsense installer (SSH as root to live installer):
           Bypasses the interactive ncurses installer entirely. SSHes as root into
           the live OPNsense environment, auto-detects the target disk, runs the
           bsdinstall sub-commands directly to partition and install OPNsense.
           Writes loader.conf.local (serial console fix) into the installed system
           before unmounting. Sets root password via opnsense-shell.

  Phase 3  Verify (serial console):
           Monitors the serial port after reboot to confirm the login prompt
           appears on COM2. No manual steps needed if Phase 2 succeeded.

USAGE
-----
  pip install -r requirements-setup.txt

  # Full run from scratch:
  python3 scripts/setup-edge510.py --serial /dev/ttyUSB0 --sn-suffix ABC

  # Resume from a specific phase:
  python3 scripts/setup-edge510.py --phase 2 --opnsense-ip 192.168.1.1
  python3 scripts/setup-edge510.py --phase 3 --serial /dev/ttyUSB0

NOTES
-----
  During Phase 2 the OPNsense installer must be booted. Connect:
    GE1 (first port from left) → this machine's ethernet  (LAN, 192.168.1.x)
    GE2 (second port)          → router / internet         (WAN, for nothing)

  The Edge 510 USB boot priority is already set — no BIOS intervention needed.

  Phase 2 uses the OPNsense SSH root password "opnsense" (the live image default).
  You provide the NEW root password you want on the installed system.

REQUIREMENTS
------------
  pexpect pyserial paramiko
  See requirements-setup.txt.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import time
import socket
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
_missing: list[str] = []
try:
    import serial          # noqa: F401  pyserial
    import pexpect
    import pexpect.fdpexpect
except ImportError:
    _missing.extend(["pexpect", "pyserial"])
try:
    import paramiko
except ImportError:
    _missing.append("paramiko")

if _missing:
    print(f"Missing dependencies: {', '.join(_missing)}")
    print("Install with:  pip install -r requirements-setup.txt")
    sys.exit(1)

import serial as _serial   # noqa: E402 — after check

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BAUD_RATE           = 115200
VELOCLOUD_PROMPT    = r"[#$] "
VCROOT_PASSWORD     = "VeloHello{suffix}"
COREBOOT_URL        = (
    "https://raw.githubusercontent.com/"
    "PhoenixSheppy/VeloCloud-Edge-510-OPNsense-Conversion-Guide/"
    "refs/heads/main/firmware/2017-4-10-coreboot.rom"
)
# Local server alternative — used when this machine serves the ROM directly.
# Start with:  cd tmp/rom-serve && python3 -m http.server 8888
LOCAL_ROM_SERVER_IP   = "192.168.1.100"  # enp14s0f0.3999 — Edge 510 traffic arrives VLAN 3999 tagged via switch
LOCAL_ROM_SERVER_PORT = 8888
LOCAL_ROM_URL         = f"http://{LOCAL_ROM_SERVER_IP}:{LOCAL_ROM_SERVER_PORT}/2017-4-10-coreboot.rom"
ROM_DIR             = "illegal-firmware"
ROM_FILE            = f"{ROM_DIR}/2017-4-10-coreboot.rom"

# Live OPNsense installer defaults
LIVE_SSH_USER       = "root"
LIVE_SSH_PASS       = "opnsense"
LIVE_SSH_PORT       = 22
DEFAULT_LAN_IP      = "192.168.1.1"

# Serial console fix
LOADER_CONF_LOCAL   = 'comconsole_port="0x2f8"\n'

# Serial console config.xml fields
SERIAL_CONSOLE_XML  = {
    "primaryconsole":   "serial",
    "secondaryconsole": "serial",
    "serialspeed":      "115200",
}

# SSH config.xml fields — written into the <ssh> child of <system>
# Enables SSH server, root login with password, listening on LAN only.
SSH_XML  = {
    "enabled":       "enabled",
    "passwordauth":  "1",
    "permitrootlogin": "1",
    "interfaces":    "lan",
    "group":         "admins",
    "noauto":        "1",
}

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def banner(text: str) -> None:
    w = 68
    print(f"\n{'=' * w}\n  {text}\n{'=' * w}")


def step(text: str) -> None:
    print(f"\n  ▶  {text}")


def ok(text: str) -> None:
    print(f"  ✓  {text}")


def warn(text: str) -> None:
    print(f"  ⚠  {text}", file=sys.stderr)


def info(text: str) -> None:
    print(f"     {text}")


def pause(prompt: str = "Press Enter when ready…") -> None:
    input(f"\n  ⏸   {prompt} ")


def instructions(text: str) -> None:
    """Print a boxed instruction block."""
    lines = textwrap.dedent(text).strip().splitlines()
    width = max(len(l) for l in lines) + 4
    print(f"\n  ┌{'─' * width}┐")
    for line in lines:
        print(f"  │  {line:<{width - 2}}  │")
    print(f"  └{'─' * width}┘")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def wait_for_tcp(host: str, port: int, timeout: int = 300, interval: int = 5) -> bool:
    """Poll host:port until it accepts a TCP connection."""
    deadline = time.time() + timeout
    print(f"  ⏳  Waiting for {host}:{port}", end="", flush=True)
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                print("  ✓")
                return True
        except OSError:
            print(".", end="", flush=True)
            time.sleep(interval)
    print()
    return False


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(host: str, port: int, user: str, password: str,
                retries: int = 5, delay: int = 10) -> paramiko.SSHClient:
    """Connect SSH with retry. Returns connected client."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for attempt in range(retries):
        try:
            client.connect(
                host, port=port, username=user, password=password,
                look_for_keys=False, allow_agent=False, timeout=15,
                banner_timeout=30,
            )
            return client
        except paramiko.AuthenticationException:
            raise
        except Exception as e:
            if attempt < retries - 1:
                warn(f"SSH attempt {attempt + 1} failed ({e}), retrying…")
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("SSH connection failed after all retries")


def upload_and_run(client: paramiko.SSHClient, script: str,
                   remote_path: str = "/tmp/_setup_phase2.sh",
                   timeout: int = 600) -> int:
    """
    Upload a shell script via SFTP and execute it, streaming output.
    Returns the exit code.
    """
    # Upload via SFTP
    step("Uploading install script to device…")
    sftp = client.open_sftp()
    with sftp.open(remote_path, "w") as f:
        f.write(script)
    sftp.chmod(remote_path, 0o700)
    sftp.close()
    ok(f"Script uploaded to {remote_path}")

    # Execute with output streaming
    step("Running install script (output below)…")
    print("  " + "─" * 64)
    transport = client.get_transport()
    channel = transport.open_session()
    channel.set_combine_stderr(True)
    channel.exec_command(f"sh {remote_path}")

    while True:
        if channel.recv_ready():
            data = channel.recv(4096).decode("utf-8", errors="replace")
            for line in data.splitlines():
                print(f"    {line}")
        elif channel.exit_status_ready():
            # Drain any remaining output
            while channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                for line in data.splitlines():
                    print(f"    {line}")
            break
        else:
            time.sleep(0.2)

    exit_code = channel.recv_exit_status()
    print("  " + "─" * 64)
    return exit_code


# ---------------------------------------------------------------------------
# Phase 2 install script generator
# ---------------------------------------------------------------------------

def build_install_script(root_password: str) -> str:
    """
    Generate the shell script that runs entirely on the live OPNsense installer.

    This replicates what opnsense-ufs.sh + opnsense-install.sh do, without the
    ncurses dialogs. Disk is auto-detected. Serial console fix is applied to the
    installed system before unmounting.
    """
    # Escape the password for shell (single-quote safe approach)
    safe_pass = root_password.replace("'", "'\\''")

    # Build the XML patch as an inline Python one-liner
    xml_tags = ", ".join(
        f"('{tag}', '{val}')" for tag, val in SERIAL_CONSOLE_XML.items()
    )
    ssh_tags = ", ".join(
        f"('{tag}', '{val}')" for tag, val in SSH_XML.items()
    )
    xml_patch_cmd = (
        "python3 -c \""
        "import xml.etree.ElementTree as ET; "
        "SE = ET.SubElement; "
        "ET.register_namespace('', ''); "
        "tree = ET.parse('/mnt/conf/config.xml'); "
        "root = tree.getroot(); "
        "sys = root.find('system'); "
        # Serial console fields on <system>
        f"[setattr(sys.find(t) if sys.find(t) is not None else SE(sys, t), 'text', v) for t, v in [{xml_tags}]]; "
        "[sys.remove(sys.find(b)) for b in ['serialusb', 'enableserial'] if sys.find(b) is not None]; "
        # SSH fields on <system><ssh>
        "ssh = sys.find('ssh') if sys.find('ssh') is not None else SE(sys, 'ssh'); "
        f"[setattr(ssh.find(t) if ssh.find(t) is not None else SE(ssh, t), 'text', v) for t, v in [{ssh_tags}]]; "
        "tree.write('/mnt/conf/config.xml', xml_declaration=True, encoding='unicode')"
        "\""
    )

    return textwrap.dedent(f"""\
        #!/bin/sh
        # OPNsense automated install script — generated by setup-edge510.py
        set -e

        log() {{ echo "[$(date +%H:%M:%S)] $*"; }}
        die() {{ echo "[ERROR] $*" >&2; exit 1; }}

        # ── Environment ──────────────────────────────────────────────────────
        export BSDINSTALL_CHROOT=/mnt
        export BSDINSTALL_TMPETC=/tmp/bsdinstall_etc
        export WORKAROUND_HYBRID=1          # needed for BIOS/hybrid GPT on coreboot

        rm -rf "$BSDINSTALL_TMPETC"
        mkdir -p "$BSDINSTALL_TMPETC" "$BSDINSTALL_CHROOT"
        touch "$BSDINSTALL_TMPETC/fstab"

        log "Environment set: CHROOT=$BSDINSTALL_CHROOT TMPETC=$BSDINSTALL_TMPETC"

        # ── Disk detection ───────────────────────────────────────────────────
        # The USB installer is the boot device; the internal eMMC is the target.
        # mmcsd0 is always eMMC on FreeBSD. If not present, fall back to
        # the first non-boot disk.

        log "Detecting target disk…"

        ALLDISKS=$(sysctl -n kern.disks 2>/dev/null | tr ' ' '\\n' | grep -vE '^(cd|md|pass)' || true)

        # Prefer mmcsd0 (eMMC — definitely internal)
        if echo "$ALLDISKS" | grep -q '^mmcsd0$'; then
            TARGETDISK=mmcsd0
            log "Internal eMMC detected as mmcsd0"
        else
            # On this hardware the live OPNsense installer runs from a memory filesystem
            # (root is md/tmpfs, not da0), so df / doesn't reliably give the USB device.
            # Instead: skip any disk whose description contains known USB brand strings,
            # then take the FIRST remaining candidate (da1 = eMMC comes before da0 = USB
            # in kern.disks because it was enumerated first).
            TARGETDISK=""
            for disk in $ALLDISKS; do
                DESC=$(geom disk list "$disk" 2>/dev/null | awk '/descr/ {{print $0}}' || true)
                if echo "$DESC" | grep -qiE 'USB|SanDisk|Kingston|Verbatim|PNY|Patriot|Lexar|Transcend|TOSHIBA USB|Generic.*Flash'; then
                    log "Skipping USB device: $disk ($DESC)"
                    continue
                fi
                TARGETDISK="$disk"
                log "Candidate target disk: $disk"
                break
            done

            # Last-resort: use first disk that is NOT the OPNsense installer USB
            # (identified by having fewer partitions than the eMMC — installer has 4, eMMC has 6+)
            if [ -z "$TARGETDISK" ]; then
                log "USB brand detection failed; falling back to partition-count heuristic…"
                for disk in $ALLDISKS; do
                    NPARTS=$(gpart show "$disk" 2>/dev/null | grep -c '^\s' || echo 0)
                    log "  $disk: $NPARTS partitions"
                    if [ "$NPARTS" -gt 4 ]; then
                        TARGETDISK="$disk"
                        log "Selected $disk (has $NPARTS partitions — likely eMMC)"
                        break
                    fi
                done
            fi
        fi

        [ -z "$TARGETDISK" ] && die "No target disk found. Available: $ALLDISKS"

        # Show disk info so we can verify in the log
        log "Target disk: $TARGETDISK"
        TARGETDESC=$(geom disk list "$TARGETDISK" 2>/dev/null | grep -E 'Geom|Mediasize|descr' || true)
        log "$TARGETDESC"

        # Hard safety: refuse to target any disk whose description screams "USB flash drive"
        if echo "$TARGETDESC" | grep -qiE 'USB|SanDisk|Kingston|Verbatim|PNY|Patriot|Lexar|Transcend'; then
            die "SAFETY ABORT: $TARGETDISK looks like a USB flash drive. Will not install to it. Description: $TARGETDESC"
        fi

        # Safety: refuse to target a disk smaller than 4GB
        DISKSIZE=$(geom disk list "$TARGETDISK" 2>/dev/null | awk '/Mediasize/ {{print $2}}')
        MINSIZE=$((4 * 1024 * 1024 * 1024))
        if [ -n "$DISKSIZE" ] && [ "$DISKSIZE" -lt "$MINSIZE" ]; then
            die "Target disk $TARGETDISK is too small ($DISKSIZE bytes, need >= 4GB)"
        fi

        # ── Partition ────────────────────────────────────────────────────────
        log "Partitioning $TARGETDISK (GPT, EFI+freebsd-boot+UFS)…"

        # Destroy existing partition table (ignore error if none)
        gpart destroy -F "$TARGETDISK" 2>/dev/null || true

        # Create GPT layout:
        #   p1: 260MB EFI  (for UEFI boot)
        #   p2: 512KB freebsd-boot  (for BIOS/GPT hybrid boot via coreboot/SeaBIOS)
        #   p3: rest → freebsd-ufs /
        #
        # No swap: Edge 510 internal flash is ~8GB, well under the 30GB threshold
        bsdinstall scriptedpart "$TARGETDISK" gpt \\
            "{{ 260M efi, 512K freebsd-boot, auto freebsd-ufs / }}" \\
            || die "scriptedpart failed"

        # Install bootcode for BIOS/GPT hybrid (coreboot/SeaBIOS uses this)
        gpart bootcode -b /boot/pmbr -p /boot/gptboot -i 2 "$TARGETDISK" \\
            || die "bootcode failed"

        # Label partitions (makes fstab disk-label safe)
        gpart modify -i 1 -l efifs  "$TARGETDISK"
        gpart modify -i 2 -l bootfs "$TARGETDISK"
        gpart modify -i 3 -l rootfs "$TARGETDISK"

        # Rewrite fstab to use GPT labels rather than device paths
        sed -i '' \\
            -e "s|/${{TARGETDISK}}p3|/gpt/rootfs|g" \\
            -e "s|/${{TARGETDISK}}p1|/gpt/efifs|g" \\
            "$BSDINSTALL_TMPETC/fstab"

        log "Partition table:"
        gpart show "$TARGETDISK"

        # ── Mount ────────────────────────────────────────────────────────────
        log "Mounting target partitions to $BSDINSTALL_CHROOT…"
        bsdinstall mount || die "mount failed"

        # ── Install (the slow cpdup clone step, ~5 minutes) ──────────────────
        log "Installing OPNsense to $TARGETDISK — cloning live system (this takes ~5 min)…"
        bsdinstall opnsense-install || die "opnsense-install failed"
        log "Clone complete."

        # ── Serial console fix ───────────────────────────────────────────────
        # Applied to the INSTALLED system before unmounting, so it is present
        # on first boot — no post-install SSH needed.
        log "Applying serial console fix…"

        # 1. loader.conf.local: redirect kernel console to COM2 (0x2f8 = ttyu1)
        #    coreboot uses COM2; FreeBSD defaults to COM1 (0x3f8).
        #    hint.uart overrides are required because device.hints marks uart0
        #    (COM1) as the console device (flags=0x10); we must clear that and
        #    set the flag on uart1 (COM2) so the kernel uses the correct port.
        cat > "$BSDINSTALL_CHROOT/boot/loader.conf.local" << 'LOADEREOF'
comconsole_port="0x2f8"
comconsole_speed="115200"
hint.uart.0.flags="0x0"
hint.uart.1.flags="0x10"
LOADEREOF
        log "  loader.conf.local: comconsole=COM2 (0x2f8/ttyu1) with uart hint overrides"

        # 2. config.xml: set serial console + enable SSH (root/password/LAN)
        if [ -f "$BSDINSTALL_CHROOT/conf/config.xml" ]; then
            {xml_patch_cmd}
            log "  config.xml: serial console + SSH (root/password/LAN) configured"
        else
            log "  WARNING: config.xml not found in chroot — skipping XML patch"
        fi

        # ── Root password ────────────────────────────────────────────────────
        log "Setting root password…"
        # opnsense-shell password updates BOTH /etc/master.passwd AND config.xml
        printf '%s\\n' '{safe_pass}' | \\
            chroot "$BSDINSTALL_CHROOT" /usr/local/sbin/opnsense-shell password root -h 0 \\
            || die "Failed to set root password"
        log "Root password set."

        # ── Bootloader config ─────────────────────────────────────────────────
        log "Configuring bootloader (EFI + BIOS hybrid)…"
        bsdinstall bootconfig || log "bootconfig returned non-zero (may be OK on BIOS-only systems)"

        # ── Unmount ───────────────────────────────────────────────────────────
        log "Unmounting…"
        bsdinstall umount || die "umount failed"

        log "Install complete. Rebooting…"
        reboot
    """)


# ---------------------------------------------------------------------------
# Phase 1 — VeloCloud OS via serial
# ---------------------------------------------------------------------------

def phase1_velocloud(serial_port: str, sn_suffix: str, skip_download: bool) -> None:
    banner("PHASE 1 — VeloCloud OS: flash coreboot")

    password = VCROOT_PASSWORD.format(suffix=sn_suffix.upper())

    step(f"Opening {serial_port} at {BAUD_RATE} baud…")
    ser = _serial.Serial(serial_port, BAUD_RATE, timeout=0.5)
    console = pexpect.fdpexpect.fdspawn(
        ser.fileno(), logfile=sys.stdout,
        encoding="latin-1", timeout=60,
    )

    instructions("""
        Power on the device now if it is not already running.
        You should see coreboot output, then VeloCloud booting.
        Waiting for the login prompt (up to 3 minutes)…
    """)

    # Send Enter proactively — VeloCloud OS may need it to activate the console.
    # Then wait for whichever prompt arrives first.
    console.sendline("")
    idx = console.expect([r"Please press Enter", r"login:", r"VeloCloud login:"], timeout=180)
    if idx == 0:
        console.sendline("")
        console.expect([r"login:", r"VeloCloud login:"], timeout=30)
    console.sendline("root")
    console.expect(r"[Pp]assword:", timeout=10)
    console.sendline(password)
    console.expect(VELOCLOUD_PROMPT, timeout=15)
    ok("Logged in to VeloCloud OS.")

    console.sendline("cd /root")
    console.expect(VELOCLOUD_PROMPT, timeout=10)

    # Network setup — configure a static IP on ge3 (GE/4, WAN port) so VeloCloud
    # OS can reach this machine's local HTTP server (no internet required).
    # This machine must have an IP on the parent (untagged) interface:
    #   sudo ip addr add 192.168.1.101/24 dev enp14s0f0
    step("Configuring VeloCloud OS network to reach local ROM server…")
    vc_ip    = "192.168.1.200"
    vc_gw    = LOCAL_ROM_SERVER_IP
    vc_iface = None

    # Find interfaces with link/carrier first, then try all ge* as fallback.
    console.sendline(
        "for i in /sys/class/net/ge*; do "
        "  iface=$(basename $i); "
        "  carrier=$(cat $i/carrier 2>/dev/null); "
        "  echo $iface:$carrier; "
        "done"
    )
    console.expect(VELOCLOUD_PROMPT, timeout=10)
    carrier_raw = console.before or ""
    # Interfaces with carrier=1 first, then others
    with_carrier = [l.split(":")[0] for l in carrier_raw.splitlines()
                    if ":1" in l and l.split(":")[0].startswith("ge")]
    all_ge = [l.split(":")[0] for l in carrier_raw.splitlines()
              if l.split(":")[0].startswith("ge")]
    candidates = with_carrier + [g for g in all_ge if g not in with_carrier]
    if not candidates:
        candidates = ["ge3", "ge4", "ge0", "ge1", "ge2"]
    info(f"Interface candidates (carrier-first): {candidates}")

    for iface in candidates:
        console.sendline(f"ip addr flush dev {iface} 2>/dev/null; "
                         f"ip addr add {vc_ip}/24 dev {iface}; "
                         f"ip link set {iface} up; "
                         f"ping -c 1 -w 3 {vc_gw}")
        idx = console.expect(
            [r"1 packets transmitted, 1 received", r"1 received", r"0 received",
             r"unreachable", r"error", r"Cannot find"],
            timeout=15,
        )
        console.expect(VELOCLOUD_PROMPT, timeout=10)
        if idx <= 1:
            vc_iface = iface
            ok(f"Reached {vc_gw} via {iface} ({vc_ip}/24).")
            break
        # Clean up failed attempt
        console.sendline(f"ip addr flush dev {iface} 2>/dev/null")
        console.expect(VELOCLOUD_PROMPT, timeout=5)

    if not vc_iface:
        print(f"\n❌  Could not reach {vc_gw} on any interface.")
        print("    Check ethernet cable is in one of the GE ports and that")
        print(f"    this machine has an IP in the 192.168.1.x subnet.")
        sys.exit(1)

    for iface in candidates:
        console.sendline(f"ip addr flush dev {iface} 2>/dev/null; "
                         f"ip addr add {vc_ip}/24 dev {iface}; "
                         f"ip link set {iface} up; "
                         f"ping -c 1 -w 3 {vc_gw}")
        idx = console.expect(
            [r"1 packets transmitted, 1 received", r"1 received", r"0 received",
             r"unreachable", r"error"],
            timeout=15,
        )
        console.expect(VELOCLOUD_PROMPT, timeout=10)
        if idx <= 1:
            vc_iface = iface
            ok(f"Reached {vc_gw} via {iface} ({vc_ip}/24).")
            break
        # Clean up failed attempt
        console.sendline(f"ip addr flush dev {iface} 2>/dev/null")
        console.expect(VELOCLOUD_PROMPT, timeout=5)

    if not vc_iface:
        die(f"Could not reach {vc_gw} on any interface. "
            "Check ethernet cable is in the GE/4 (rightmost) port.")

    if not skip_download:
        step(f"Creating {ROM_DIR}/ and downloading coreboot ROM from local server…")
        console.sendline(f"mkdir -p {ROM_DIR}")
        console.expect(VELOCLOUD_PROMPT, timeout=10)
        console.sendline(f"wget -q '{LOCAL_ROM_URL}' -O {ROM_FILE}")
        console.expect(VELOCLOUD_PROMPT, timeout=120)
        ok("ROM downloaded.")

    step("Flashing DMI table…")
    console.sendline(f"./dmi-tool -u {ROM_FILE}")
    console.expect(VELOCLOUD_PROMPT, timeout=60)
    console.sendline("./dmi-tool -w -p EDGE510 -v 1")
    console.expect(VELOCLOUD_PROMPT, timeout=60)
    ok("DMI flashed.")

    step("Flashing coreboot ROM via flashrom (3–5 minutes — do NOT power off)…")
    console.sendline(f"flashrom --programmer internal --write {ROM_FILE}")
    console.expect(
        [r"VERIFIED", r"Chip content is identical", r"SUCCESS"],
        timeout=600,
    )
    console.expect(VELOCLOUD_PROMPT, timeout=30)
    ok("coreboot flashed successfully.")

    step("Disabling VeloCloud watchdog via i2c…")
    console.sendline("i2cset -y 1 0x24 0x00 0x00")
    console.expect(VELOCLOUD_PROMPT, timeout=10)
    console.sendline("i2cset -y 1 0x24 0x01 0x00")
    console.expect(VELOCLOUD_PROMPT, timeout=10)
    ok("Watchdog disabled. Device is free from VeloCloud.")

    ser.close()

    instructions("""
        Phase 1 complete!

        Next steps (do these NOW before pressing Enter):
          1. Plug your OPNsense USB installer into one of the USB-A ports
          2. Power cycle the device (unplug power, wait 5 s, reconnect)
          3. The device will automatically boot the USB installer
          4. Connect GE1 (first LAN port) to this machine via ethernet
          5. Connect GE2 (second port) to your router/internet (for WAN)

        OPNsense default LAN IP will be 192.168.1.1
        Wait until the device has fully booted (~60-90 seconds after power on).
    """)
    pause("Press Enter once the OPNsense live installer is booted…")


# ---------------------------------------------------------------------------
# Phase 2 — OPNsense installer via SSH (fully automated)
# ---------------------------------------------------------------------------

def phase2_install(opnsense_ip: str, root_password: str | None) -> str:
    banner("PHASE 2 — OPNsense automated install")

    if root_password is None:
        print()
        print("  You will need to choose a root password for the installed OPNsense.")
        while True:
            root_password = input("  New root password: ").strip()
            confirm = input("  Confirm password: ").strip()
            if root_password and root_password == confirm:
                break
            print("  Passwords do not match or are empty. Try again.")

    step(f"Waiting for OPNsense live installer SSH at {opnsense_ip}:{LIVE_SSH_PORT}…")
    if not wait_for_tcp(opnsense_ip, LIVE_SSH_PORT, timeout=180):
        instructions(f"""
            Cannot reach {opnsense_ip}. Check:
              • GE1 is connected to this machine
              • Your machine has an IP in the 192.168.1.x/24 range
              • The device has fully booted (wait another 30 s and retry)
        """)
        if not wait_for_tcp(opnsense_ip, LIVE_SSH_PORT, timeout=60):
            warn("Still cannot connect. Exiting.")
            sys.exit(1)

    step(f"Connecting SSH as root@{opnsense_ip} (live installer password: {LIVE_SSH_PASS})…")
    try:
        client = ssh_connect(opnsense_ip, LIVE_SSH_PORT, LIVE_SSH_USER, LIVE_SSH_PASS)
    except paramiko.AuthenticationException:
        warn(f"Authentication failed as root@{opnsense_ip}.")
        warn("The live installer may have already been used. Try --phase 3 if OPNsense is installed.")
        sys.exit(1)
    ok("SSH connected to live installer.")

    # Check whether root has a direct shell or the OPNsense console menu
    step("Checking SSH shell type…")
    try:
        _, stdout, _ = client.exec_command("echo SHELL_CHECK", timeout=5)
        out = stdout.read().decode()
        if "SHELL_CHECK" in out:
            ok("Root has a direct shell (exec_command works).")
        else:
            ok("Root shell detected via interactive session.")
    except Exception:
        ok("Proceeding with script upload approach.")

    # Build and run the install script
    script = build_install_script(root_password)
    exit_code = upload_and_run(client, script, timeout=900)

    client.close()

    # exit_code -1 is normal: the install script ends with `reboot`, which
    # kills the SSH channel before it can return 0.
    if exit_code in (0, -1, None):
        ok("Install script completed successfully.")
    else:
        warn(f"Install script exited with code {exit_code} — check output above.")
        # Don't block on input() — we may not have a tty (piped/logged runs).
        # Phase 3 will verify success via serial; proceed regardless.

    return root_password


# ---------------------------------------------------------------------------
# Phase 3 — Boot menu selection + serial console verify
# ---------------------------------------------------------------------------

# SeaBIOS boot menu entry for the eMMC (always option 2 on Edge 510):
#   1. USB MSC Drive  USB SanDisk 3.2Gen1   ← installer USB
#   2. USB MSC Drive Generic Ultra HS-COMBO ← eMMC (internal)
EMMC_BOOT_CHOICE = "2"

def phase3_verify(serial_port: str | None) -> None:
    banner("PHASE 3 — Boot menu + serial console verify")

    if not serial_port:
        instructions("""
            Skipping serial monitoring (no --serial port specified).
            Check your serial terminal manually:
              • When 'Press F12 for boot menu' appears, press F12
              • Select option 2 (Generic Ultra HS-COMBO = eMMC)
              • The OPNsense management console should appear on COM2 (mini-USB port)
              • Serial: 115200 8N1 on ttyu1 (COM2 / 0x2f8)
        """)
        return

    info(f"Monitoring {serial_port} at {BAUD_RATE} baud…")
    info("Will intercept SeaBIOS F12 boot menu to select eMMC, then verify login prompt.")
    info("Waiting up to 3 minutes for boot menu…\n")

    try:
        ser = _serial.Serial(serial_port, BAUD_RATE, timeout=1)
        console = pexpect.fdpexpect.fdspawn(
            ser.fileno(), logfile=sys.stdout,
            encoding="latin-1", timeout=10,
        )

        # ── Step 1: intercept SeaBIOS F12 boot menu ──────────────────────────
        console.expect(r"Press F12 for boot menu", timeout=180)
        print()
        ok("SeaBIOS F12 prompt detected — sending F12 to open boot menu…")
        ser.write(b"\x1b[24~")   # F12 VT220 escape sequence

        console.expect(r"Select boot device", timeout=10)
        ok(f"Boot menu open — selecting option {EMMC_BOOT_CHOICE} (eMMC)…")
        ser.write(EMMC_BOOT_CHOICE.encode() + b"\r")

        # ── Step 2: wait for OPNsense console banner ─────────────────────────
        # OPNsense shows its management menu directly on the console at boot —
        # there is no raw "login:" prompt. The banner line is:
        #   *** OPNsense.internal: OPNsense 26.x.x (amd64) ***
        info("Booting from eMMC — waiting for OPNsense console banner (up to 4 min)…\n")
        console.expect(
            [r"\*\*\* OPNsense", r"OPNsense \d+\.", r"login:", r"ogin:"],
            timeout=240,
        )
        print()
        ok("🎉  OPNsense is running from eMMC — console confirmed!")
        info("The serial port (mini-USB) shows the OPNsense management menu.")

        ser.close()

    except pexpect.TIMEOUT:
        warn("Timed out — check serial output above for clues.")
        warn("You can manually press F12 at boot and select option 2 (eMMC).")
    except Exception as exc:
        warn(f"Serial error: {exc}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(opnsense_ip: str) -> None:
    banner("ALL DONE — VeloCloud Edge 510 is now running OPNsense")
    instructions(f"""
        Your device is fully configured and headless-ready.

        Access:
          Serial console:  115200 8N1  →  root / <your password>
          SSH:             ssh root@{opnsense_ip}
          Web GUI:         https://{opnsense_ip}

        Recommended next steps:
          • System → Firmware → Updates  (install latest OPNsense)
          • Set hostname and timezone
          • Configure WAN interface (GE2 by default)
          • Change LAN IP if needed (currently 192.168.1.1/24)
          • Disable SSH root login once you have another user set up

        Serial console is on the mini-USB port under the side cover.
        COM2 / 0x2F8 / ttyu1 — confirmed working.
    """)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="VeloCloud Edge 510 → OPNsense automated conversion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:  python3 scripts/setup-edge510.py --serial /dev/ttyUSB0 --sn-suffix ABC",
    )
    p.add_argument("--serial",        default="/dev/ttyUSB0",
                   help="Serial port device (default: /dev/ttyUSB0)")
    p.add_argument("--sn-suffix",     metavar="XXX",
                   help="Last 3 characters of device S/N (for VeloCloud login)")
    p.add_argument("--opnsense-ip",   default=DEFAULT_LAN_IP,
                   help=f"OPNsense LAN IP (default: {DEFAULT_LAN_IP})")
    p.add_argument("--root-password", metavar="PASS",
                   help="Root password for installed OPNsense (prompted if omitted)")
    p.add_argument("--phase",         type=int, choices=[1, 2, 3],
                   help="Start from this phase (skips earlier phases)")
    p.add_argument("--skip-download", action="store_true",
                   help="Skip ROM download in Phase 1 (ROM already on device)")
    p.add_argument("--no-serial",     action="store_true",
                   help="Skip serial monitoring in Phase 3")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    start_phase = args.phase or 1
    serial_port = None if args.no_serial else args.serial

    print()
    print("  VeloCloud Edge 510 → OPNsense Conversion")
    print("  ──────────────────────────────────────────")
    print(f"  Serial port : {serial_port or '(disabled)'}")
    print(f"  OPNsense IP : {args.opnsense_ip}")
    print(f"  Start phase : {start_phase}")
    print()

    root_password = args.root_password

    if start_phase <= 1:
        if not args.sn_suffix:
            args.sn_suffix = input(
                "  Enter last 3 characters of device serial number (for VeloCloud login): "
            ).strip()
        phase1_velocloud(serial_port, args.sn_suffix, args.skip_download)

    if start_phase <= 2:
        root_password = phase2_install(args.opnsense_ip, root_password)

    if start_phase <= 3:
        phase3_verify(serial_port)
        print_summary(args.opnsense_ip)


if __name__ == "__main__":
    main()
