r"""Raspberry Pi OS installer for the hokku-server.

Downloads/caches a Pi OS 64-bit Lite image, writes it to an SD card, injects
firstrun hooks that configure wifi, user, SSH, avahi, samba (optional), and
the hokku-server .deb on first boot. Then waits for the Pi to come up on
mDNS and answer HTTP.

Windows-only (uses \\.\PhysicalDriveN and wmic). Admin required.
"""
import ctypes
import ctypes.wintypes as wt
import getpass
import json
import lzma
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import release_cache

REPO_ROOT = release_cache.REPO_ROOT
CACHE_DIR = release_cache.CACHE_DIR
PI_OS_HOSTNAME = "hokku-server"
PI_OS_DEFAULT_USER = "hokku"
PI_OS_DEFAULT_PASS = "hokku"
WEBSERVER_PORT = 8080
WEBSERVER_WAIT_SECS = 60

PI_OS_LATEST_URL = "https://downloads.raspberrypi.com/raspios_lite_arm64_latest"

# Windows FSCTLs
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# ---------- input validation ----------

# Shared safe character set: printable ASCII (0x20-0x7E) minus characters that
# are hard to embed safely across the four contexts we interpolate into:
# bash double-quoted strings, bash heredocs, NetworkManager .nmconnection ini
# values, and wpa_supplicant double-quoted string values.
_DISALLOWED_ANY = set('"\\\n\r')


def _bad_chars(s, extra_disallowed=""):
    """Return sorted list of disallowed/unprintable characters found in `s`."""
    bad = set()
    for ch in s:
        code = ord(ch)
        if code < 0x20 or code > 0x7E:
            bad.add(ch)
        elif ch in _DISALLOWED_ANY or ch in extra_disallowed:
            bad.add(ch)
    return sorted(bad)


def _char_report(chars):
    """Human-readable list of bad characters — shows repr so control chars visible."""
    return ", ".join(repr(c) for c in chars)


def validate_ssid(s):
    """Return (ok, reason). WPA SSID: 1-32 bytes, no `"`, `\\`, newlines, no non-printable."""
    if not s:
        return False, "SSID is empty"
    if len(s.encode("utf-8")) > 32:
        return False, f"SSID is {len(s.encode('utf-8'))} bytes (max 32)"
    bad = _bad_chars(s)
    if bad:
        return False, f"SSID contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_wifi_password(s):
    """Return (ok, reason). WPA2 PSK: 8-63 printable ASCII (or empty for open network)."""
    if s == "":
        return True, ""  # open network
    if len(s) < 8:
        return False, "WiFi password must be at least 8 characters (WPA2 PSK requirement)"
    if len(s) > 63:
        return False, f"WiFi password is {len(s)} characters (max 63)"
    bad = _bad_chars(s)
    if bad:
        return False, f"WiFi password contains disallowed characters: {_char_report(bad)}"
    return True, ""


def validate_username(s):
    """Return (ok, reason). Linux username: [a-z][a-z0-9_-]*, max 32."""
    if not s:
        return False, "Username is empty"
    if len(s) > 32:
        return False, f"Username is {len(s)} characters (max 32)"
    if not s[0].islower() and s[0] != "_":
        return False, "Username must start with a lowercase letter or underscore"
    for ch in s:
        if not (ch.islower() or ch.isdigit() or ch in "_-"):
            return False, f"Username contains disallowed character: {ch!r} (allowed: a-z 0-9 _ -)"
    return True, ""


def validate_linux_password(s):
    """Return (ok, reason). chpasswd line: no `:` (separator), no newline/CR."""
    if not s:
        return False, "Password is empty"
    bad = _bad_chars(s, extra_disallowed=":")
    if bad:
        return False, f"Password contains disallowed characters: {_char_report(bad)}"
    return True, ""


# ---------- Windows helpers ----------

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def fmt_gb(n):
    if n <= 0:
        return "?"
    return f"{n / 1024**3:.1f} GB"


# ---------- Drive listing (PowerShell primary, wmic fallback) ----------

_POWERSHELL_DRIVES = r'''
$out = @()
foreach ($d in Get-CimInstance Win32_DiskDrive) {
    $letters = @()
    try {
        $parts = Get-CimInstance -Query ("ASSOCIATORS OF {Win32_DiskDrive.DeviceID=`"" + $d.DeviceID.Replace('\','\\') + "`"} WHERE AssocClass = Win32_DiskDriveToDiskPartition")
        foreach ($p in $parts) {
            $ld = Get-CimInstance -Query ("ASSOCIATORS OF {Win32_DiskPartition.DeviceID=`"" + $p.DeviceID + "`"} WHERE AssocClass = Win32_LogicalDiskToPartition")
            if ($ld) { $letters += $ld.DeviceID }
        }
    } catch {}
    $out += [PSCustomObject]@{
        Index = [int]$d.Index
        Model = [string]$d.Model
        Size = [int64]($d.Size)
        InterfaceType = [string]$d.InterfaceType
        MediaType = [string]$d.MediaType
        Letters = $letters
    }
}
$out | ConvertTo-Json -Compress -Depth 3
'''


def _run_powershell_drives():
    """Return raw stdout from the PowerShell drives query, or None if PS unavailable/failed."""
    for exe in ("powershell.exe", "pwsh.exe"):
        try:
            res = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_DRIVES],
                capture_output=True, text=True, timeout=15,
            )
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout
    return None


def parse_powershell_drives(stdout):
    """Parse the JSON output of _POWERSHELL_DRIVES into list of drive dicts."""
    if not stdout or not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]  # single-row JSON is not wrapped in a list
    drives = []
    for r in data:
        letters = r.get("Letters") or []
        if isinstance(letters, str):
            letters = [letters]
        media = r.get("MediaType") or ""
        removable = "Removable" in media or "External" in media
        drives.append({
            "index": int(r.get("Index", -1)),
            "model": (r.get("Model") or "").strip(),
            "size_bytes": int(r.get("Size") or 0),
            "interface": (r.get("InterfaceType") or "").strip(),
            "media": media,
            "removable": removable,
            "letters": list(letters),
        })
    return drives


def _wmic(command):
    """Run wmic, return stdout text (empty on failure or wmic missing)."""
    try:
        res = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return res.stdout if res.returncode == 0 else ""
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def parse_wmic_table(output):
    """Parse fixed-width wmic /format:table output into list of dicts."""
    lines = [l.rstrip() for l in output.splitlines() if l.strip()]
    if len(lines) < 2:
        return []
    header_line = lines[0]
    positions = []
    in_space = True
    for i, ch in enumerate(header_line):
        if in_space and ch != " ":
            positions.append(i)
            in_space = False
        elif not in_space and ch == " ":
            in_space = True
    col_names = []
    for idx, start in enumerate(positions):
        end = positions[idx + 1] if idx + 1 < len(positions) else len(header_line)
        col_names.append(header_line[start:end].strip())
    rows = []
    for line in lines[1:]:
        row = {}
        for idx, start in enumerate(positions):
            end = positions[idx + 1] if idx + 1 < len(positions) else len(line)
            row[col_names[idx]] = line[start:end].strip() if start < len(line) else ""
        rows.append(row)
    return rows


def _wmic_list_drive_letters_for(disk_index):
    """wmic-based partition→letter mapping for one physical disk."""
    out = _wmic([
        "wmic", "path", "Win32_DiskDriveToDiskPartition", "get",
        "Antecedent,Dependent", "/format:list"
    ])
    partitions = []
    for blk in out.replace("\r", "").split("\n\n"):
        d = {}
        for line in blk.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
        ante = d.get("Antecedent", "")
        dep = d.get("Dependent", "")
        if 'DeviceID="' not in ante or 'DeviceID="' not in dep:
            continue
        try:
            disk_n = int(ante.split('DeviceID="')[1].split('"')[0].split("\\\\")[-1].replace("PHYSICALDRIVE", ""))
        except Exception:
            continue
        part_id = dep.split('DeviceID="')[1].split('"')[0]
        partitions.append((disk_n, part_id))

    out2 = _wmic([
        "wmic", "path", "Win32_LogicalDiskToPartition", "get",
        "Antecedent,Dependent", "/format:list"
    ])
    letters = []
    for blk in out2.replace("\r", "").split("\n\n"):
        d = {}
        for line in blk.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
        ante = d.get("Antecedent", "")
        dep = d.get("Dependent", "")
        if 'DeviceID="' not in ante or 'DeviceID="' not in dep:
            continue
        part_id = ante.split('DeviceID="')[1].split('"')[0]
        letter = dep.split('DeviceID="')[1].split('"')[0]
        for (di, pid) in partitions:
            if di == disk_index and pid == part_id:
                letters.append(letter)
    return letters


def list_disk_drives():
    """PowerShell first, wmic fallback. Returns list of drive dicts w/ letters filled in."""
    ps_out = _run_powershell_drives()
    if ps_out is not None:
        return parse_powershell_drives(ps_out)

    # Fallback: wmic
    out = _wmic(["wmic", "diskdrive", "get",
                 "Index,Model,Size,InterfaceType,MediaType", "/format:table"])
    rows = parse_wmic_table(out)
    drives = []
    for r in rows:
        try:
            idx = int(r.get("Index", ""))
        except ValueError:
            continue
        try:
            size = int(r.get("Size", "") or 0)
        except ValueError:
            size = 0
        media = r.get("MediaType", "")
        removable = "Removable" in media or "External" in media
        drives.append({
            "index": idx,
            "model": r.get("Model", ""),
            "size_bytes": size,
            "interface": r.get("InterfaceType", ""),
            "media": media,
            "removable": removable,
            "letters": _wmic_list_drive_letters_for(idx),
        })
    return drives


def list_drive_letters_for(disk_index):
    """Convenience: letters for a single disk, using whichever backend is active."""
    for d in list_disk_drives():
        if d["index"] == disk_index:
            return list(d.get("letters") or [])
    return []


def guess_sd_drive(drives):
    """Pick the most SD-card-like drive. Returns drive dict or None."""
    candidates = [d for d in drives if d["removable"] and 2 * 1024**3 <= d["size_bytes"] <= 256 * 1024**3]
    if not candidates:
        return None
    # Prefer USB interface
    candidates.sort(key=lambda d: (d["interface"] != "USB", d["size_bytes"]))
    return candidates[0]


def wait_for_new_drive(initial_indices, prompt):
    """Poll wmic every 2s for a new disk. Returns the new drive dict when one appears."""
    print(f"  {prompt}")
    print("  (scanning every 2s; Ctrl-C to cancel)")
    spin = "|/-\\"
    i = 0
    while True:
        drives = list_disk_drives()
        new = [d for d in drives if d["index"] not in initial_indices]
        if new:
            # Pick the first removable new drive
            removable = [d for d in new if d["removable"]]
            picked = removable[0] if removable else new[0]
            print(f"  New drive detected: PhysicalDrive{picked['index']} "
                  f"— {picked['model']} ({fmt_gb(picked['size_bytes'])})")
            return picked
        sys.stdout.write(f"\r  waiting {spin[i % 4]} ")
        sys.stdout.flush()
        i += 1
        time.sleep(2)


def prompt_sd_drive():
    """Step 1: ask user to insert SD card, then pick/confirm a drive."""
    print()
    print("  Step 1 of 4: Select SD card drive")
    print("  --------------------------------")
    initial = list_disk_drives()
    initial_idx = {d["index"] for d in initial}

    guess = guess_sd_drive(initial)

    if guess:
        letters = ", ".join(guess.get("letters") or []) or "no letter"
        print(f"  Detected likely SD card: PhysicalDrive{guess['index']} — "
              f"{guess['model']} ({fmt_gb(guess['size_bytes'])}, {letters})")
        print()
        print(f"    [Enter]  use PhysicalDrive{guess['index']}")
        print("    l        list all drives and pick manually")
        print("    w        wait for a different SD card to be inserted")
        resp = input("  Choice: ").strip().lower()
        if resp == "":
            return guess
        if resp == "l":
            _print_drive_list(list_disk_drives())
            idx = _prompt_drive_index()
            return next((d for d in list_disk_drives() if d["index"] == idx), None)
        if resp == "w":
            return wait_for_new_drive(initial_idx, "Waiting for a new SD card...")
        print(f"  Unknown choice {resp!r}, aborting.")
        return None

    # No guess — ask user to insert one.
    print("  No removable SD-card-sized drive currently visible.")
    print()
    print("    [Enter]  wait for an SD card to be inserted")
    print("    l        list all drives and pick manually")
    resp = input("  Choice: ").strip().lower()
    if resp == "l":
        _print_drive_list(list_disk_drives())
        idx = _prompt_drive_index()
        return next((d for d in list_disk_drives() if d["index"] == idx), None)
    return wait_for_new_drive(initial_idx, "Waiting for an SD card...")


def _print_drive_list(drives):
    print()
    print("  All disk drives:")
    for d in drives:
        letters = ", ".join(d.get("letters") or []) or "(no letter)"
        removable = "removable" if d["removable"] else "fixed"
        print(f"    PhysicalDrive{d['index']}  {d['model']}  {fmt_gb(d['size_bytes'])}  "
              f"{d['interface']}/{removable}  [{letters}]")


def _prompt_drive_index():
    while True:
        s = input("  Enter PhysicalDrive index: ").strip()
        try:
            return int(s)
        except ValueError:
            print("  Not a number.")


# ---------- Image download/cache ----------

def ensure_cache_dir():
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR


def prompt_image_path():
    """Step 2: find/download the Pi OS image. Returns Path to .img.xz (or .img)."""
    print()
    print("  Step 2 of 4: Pi OS 64-bit Lite image")
    print("  -----------------------------------")
    ensure_cache_dir()
    cached = sorted(CACHE_DIR.glob("*raspios*arm64*lite*.img*")) + sorted(CACHE_DIR.glob("*.img.xz"))
    cached = [p for p in cached if p.is_file()]

    if cached:
        print(f"  Cached image available: {cached[-1].name}")
        print("  Press Enter to use it, or paste a path to a different .img/.img.xz file.")
    else:
        print("  No Pi OS image cached yet.")
        print("  Press Enter to download the latest Pi OS Lite 64-bit image")
        print("    (~550 MB compressed — .img.xz from downloads.raspberrypi.com,")
        print(f"     cached to {CACHE_DIR}\\ for future runs),")
        print("  or paste a path to an existing .img / .img.xz file.")

    path_str = input("  > ").strip().strip('"').strip("'")
    if path_str:
        p = Path(path_str)
        if not p.exists():
            print(f"  ERROR: {p} not found.")
            return None
        return p

    if cached:
        print(f"  Using cached image: {cached[-1]}")
        return cached[-1]

    # Download latest
    print()
    print("  Resolving latest Pi OS Lite 64-bit URL...")
    try:
        req = urllib.request.Request(PI_OS_LATEST_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            url = r.geturl()
    except Exception as e:
        print(f"  ERROR: could not resolve download URL: {e}")
        return None
    fname = url.rsplit("/", 1)[-1]
    target = CACHE_DIR / fname
    print(f"  Downloading {fname} to {CACHE_DIR}/ (may take a few minutes)...")
    if not _download_with_progress(url, target):
        return None
    return target


def _download_with_progress(url, dest):
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0))
            got = 0
            last = time.time()
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if now - last > 0.5:
                        if total:
                            pct = got * 100 // total
                            sys.stdout.write(f"\r    {pct:3d}%  {got / 1024**2:.1f} / {total / 1024**2:.1f} MB")
                        else:
                            sys.stdout.write(f"\r    {got / 1024**2:.1f} MB")
                        sys.stdout.flush()
                        last = now
        print()
        tmp.rename(dest)
        return True
    except Exception as e:
        print(f"\n  ERROR: download failed: {e}")
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


# ---------- Install config prompt ----------

def _yesno(prompt, default_yes=True):
    suffix = "[Y/n]" if default_yes else "[y/N]"
    v = input(f"  {prompt} {suffix}: ").strip().lower()
    if not v:
        return default_yes
    return v in ("y", "yes")


def _prompt_validated(prompt, validator, hidden=False, default=None):
    """Prompt until the user supplies input that passes `validator(s) -> (ok, reason)`."""
    while True:
        if hidden:
            s = getpass.getpass(prompt)
        else:
            s = input(prompt)
        if not s and default is not None:
            s = default
        ok, reason = validator(s)
        if ok:
            return s
        print(f"  ERROR: {reason}")


def collect_install_config():
    """Ask user for wifi, user/pass, ssh, samba. Returns dict."""
    print()
    print("  Install settings")
    print("  ----------------")
    print("  (printable ASCII only; `\"`, `\\`, newlines are not allowed)")

    wifi_ssid = _prompt_validated("  WiFi SSID: ", validate_ssid)
    wifi_pass = _prompt_validated("  WiFi Password (input hidden, empty = open network): ",
                                  validate_wifi_password, hidden=True)
    if not wifi_pass:
        print("  WARNING: empty WiFi password — open network assumed.")

    user = _prompt_validated(
        f"  Linux username [{PI_OS_DEFAULT_USER}]: ",
        validate_username,
        default=PI_OS_DEFAULT_USER,
    )
    while True:
        password = _prompt_validated(
            f"  Password for '{user}' [{PI_OS_DEFAULT_PASS}]: ",
            validate_linux_password,
            hidden=True,
            default=PI_OS_DEFAULT_PASS,
        )
        confirm = getpass.getpass("  Confirm password: ") or PI_OS_DEFAULT_PASS
        if password == confirm:
            break
        print("  Passwords don't match, try again.")

    ssh_enabled = _yesno("Enable SSH login?", default_yes=False)
    samba = _yesno("Install Samba (Windows file share) with same credentials?", default_yes=False)

    return {
        "hostname": PI_OS_HOSTNAME,
        "wifi_ssid": wifi_ssid,
        "wifi_pass": wifi_pass,
        "user": user,
        "password": password,
        "ssh_enabled": ssh_enabled,
        "samba": samba,
        # server_ip is populated later, after mDNS resolves hokku-server.local.
        "server_ip": None,
    }


# ---------- Raw disk write ----------

def _open_physical_drive_for_write(index):
    r"""Open \\.\PhysicalDriveN with GENERIC_READ|WRITE. Returns handle."""
    path = f"\\\\.\\PhysicalDrive{index}"
    h = ctypes.windll.kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if h == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise OSError(f"CreateFileW({path}) failed, error={err} (admin required?)")
    return h


def _open_volume(letter):
    path = f"\\\\.\\{letter.rstrip(':')}:"
    h = ctypes.windll.kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if h == INVALID_HANDLE_VALUE:
        return None
    return h


def _ioctl(handle, code):
    returned = wt.DWORD(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        handle, code, None, 0, None, 0, ctypes.byref(returned), None,
    )
    return bool(ok)


def _close(handle):
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)


def _dismount_volumes(disk_index):
    """Lock+dismount all logical volumes on the given physical disk. Returns list of open handles
    we must keep open to retain the lock during writing."""
    letters = list_drive_letters_for(disk_index)
    handles = []
    for letter in letters:
        h = _open_volume(letter)
        if not h:
            continue
        _ioctl(h, FSCTL_LOCK_VOLUME)
        _ioctl(h, FSCTL_DISMOUNT_VOLUME)
        handles.append(h)
    return handles


def _unlock_volumes(handles):
    for h in handles:
        try:
            _ioctl(h, FSCTL_UNLOCK_VOLUME)
        finally:
            _close(h)


def write_image_to_disk(image_path, disk_index, disk_size_bytes):
    """Decompress (if .xz) and raw-write `image_path` to PhysicalDrive<disk_index>.
    Prints progress. Returns True on success."""
    is_xz = str(image_path).endswith(".xz")
    src_size = image_path.stat().st_size  # only useful for .img; for .xz we don't know decompressed size

    print()
    print(f"  Writing {image_path.name} to PhysicalDrive{disk_index} ...")
    print("  This takes 3-10 minutes. Do not remove the card.")

    vol_handles = _dismount_volumes(disk_index)
    disk_h = None
    try:
        disk_h = _open_physical_drive_for_write(disk_index)

        chunk_size = 4 * 1024 * 1024
        written = 0
        last_report = time.time()

        src = open(image_path, "rb")
        try:
            if is_xz:
                dec = lzma.LZMADecompressor()
                pending = b""
                while True:
                    raw = src.read(chunk_size)
                    if not raw and dec.eof:
                        break
                    if raw:
                        pending += dec.decompress(raw)
                    # write in 4MB aligned chunks
                    while len(pending) >= chunk_size:
                        buf = pending[:chunk_size]
                        pending = pending[chunk_size:]
                        _raw_write(disk_h, buf)
                        written += len(buf)
                        if time.time() - last_report > 1.0:
                            sys.stdout.write(f"\r    written {written / 1024**2:.1f} MB")
                            sys.stdout.flush()
                            last_report = time.time()
                    if not raw:
                        break
                if pending:
                    # pad to 512-byte sector
                    if len(pending) % 512:
                        pending += b"\x00" * (512 - (len(pending) % 512))
                    _raw_write(disk_h, pending)
                    written += len(pending)
            else:
                while True:
                    buf = src.read(chunk_size)
                    if not buf:
                        break
                    if len(buf) % 512:
                        buf += b"\x00" * (512 - (len(buf) % 512))
                    _raw_write(disk_h, buf)
                    written += len(buf)
                    if time.time() - last_report > 1.0:
                        pct = written * 100 // src_size if src_size else 0
                        sys.stdout.write(f"\r    {pct:3d}%  {written / 1024**2:.1f} / {src_size / 1024**2:.1f} MB")
                        sys.stdout.flush()
                        last_report = time.time()
        finally:
            src.close()

        # flush
        ctypes.windll.kernel32.FlushFileBuffers(disk_h)
        print(f"\n    done: {written / 1024**2:.1f} MB written")

        # Tell Windows to rescan partitions
        _ioctl(disk_h, IOCTL_DISK_UPDATE_PROPERTIES)
        return True
    except Exception as e:
        print(f"\n  ERROR during write: {e}")
        return False
    finally:
        _close(disk_h)
        _unlock_volumes(vol_handles)


def _raw_write(handle, buf):
    written = wt.DWORD(0)
    ok = ctypes.windll.kernel32.WriteFile(
        handle, buf, len(buf), ctypes.byref(written), None,
    )
    if not ok or written.value != len(buf):
        err = ctypes.get_last_error()
        raise OSError(f"WriteFile failed (wrote {written.value}/{len(buf)}, err={err})")


# ---------- Boot partition customization ----------

def find_bootfs_letter(disk_index, timeout=30):
    """After imaging, Windows re-mounts the FAT bootfs partition. Poll for its letter."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        letters = list_drive_letters_for(disk_index)
        for letter in letters:
            # Look for cmdline.txt as a marker of the boot partition
            p = Path(f"{letter}\\cmdline.txt")
            if p.exists():
                return letter
        time.sleep(1)
    return None


def find_deb_package():
    """Find a hokku-server_*.deb in the usual search locations (no prompting)."""
    candidates = []
    for d in [CACHE_DIR, REPO_ROOT, REPO_ROOT / "webserver"]:
        if d.exists():
            candidates.extend(sorted(d.glob("hokku-server_*.deb")))
    return candidates[-1] if candidates else None


def _deb_name_matches(name):
    return name.startswith("hokku-server_") and name.endswith(".deb")


def fetch_latest_release_deb():
    """Query GitHub's latest-release API and download the hokku-server .deb
    to .cache/. Returns the cached Path or None on any failure."""
    print("  Querying GitHub latest release...")
    try:
        release = release_cache.get_latest_release()
    except urllib.error.HTTPError as e:
        print(f"  ERROR: GitHub API returned {e.code} {e.reason}")
        return None
    except Exception as e:
        print(f"  ERROR: could not reach GitHub: {e}")
        return None

    tag = release.get("tag_name", "?")
    asset = release_cache.find_asset(release, _deb_name_matches)
    if asset is None:
        print(f"  ERROR: release {tag} has no hokku-server_*.deb asset.")
        return None
    cached = release_cache.ensure_cached_asset(asset, CACHE_DIR, label=f"(release {tag})")
    return cached


def locate_deb_package_interactive():
    """Auto-find the .deb, or fetch it from the latest GitHub release; if both
    fail, ask the user for a path. Returns Path or None."""
    deb = find_deb_package()
    if deb:
        return deb

    print()
    print("  No hokku-server_*.deb found locally. Fetching latest GitHub release...")
    deb = fetch_latest_release_deb()
    if deb:
        return deb

    print()
    print("  Could not auto-fetch the .deb. A .deb is required to install")
    print("  hokku-server on the Pi. Options:")
    print("    - Check your internet connection and retry")
    print("    - Build one on a Linux box: cd webserver && ./build-deb.sh")
    print("    - Enter the path to an existing .deb below")
    print()

    while True:
        path_str = input("  Path to hokku-server_*.deb (blank to abort): ").strip().strip('"').strip("'")
        if not path_str:
            return None
        p = Path(path_str)
        if not p.exists():
            print(f"  ERROR: {p} does not exist.")
            continue
        if not p.is_file():
            print(f"  ERROR: {p} is not a file.")
            continue
        if not p.name.startswith("hokku-server_") or not p.suffix == ".deb":
            print(f"  ERROR: {p.name} doesn't look like a hokku-server .deb (expected hokku-server_*.deb).")
            if not _yesno("Use it anyway?", default_yes=False):
                continue
        # Cache it so future runs find it automatically.
        try:
            ensure_cache_dir()
            cached = CACHE_DIR / p.name
            if cached.resolve() != p.resolve():
                shutil.copy2(p, cached)
                print(f"  Cached to {cached} for future runs.")
            return cached
        except Exception as e:
            print(f"  (Could not cache to .cache/: {e}. Using original path.)")
            return p


def inject_boot_customization(bootfs_letter, cfg, deb_path):
    """Write firstrun.sh, firstboot-install.sh, copy .deb, patch cmdline.txt."""
    boot = Path(f"{bootfs_letter}\\")
    hokku_dir = boot / "hokku"
    hokku_dir.mkdir(exist_ok=True)

    # Copy .deb
    deb_target = hokku_dir / "hokku-server.deb"
    shutil.copy2(deb_path, deb_target)
    print(f"    copied {deb_path.name} -> {deb_target}")

    # Generate firstrun.sh and firstboot-install.sh
    firstrun = _render_firstrun(cfg)
    firstboot = _render_firstboot(cfg)

    (boot / "firstrun.sh").write_bytes(firstrun.encode("utf-8").replace(b"\r\n", b"\n"))
    (hokku_dir / "firstboot-install.sh").write_bytes(firstboot.encode("utf-8").replace(b"\r\n", b"\n"))
    print("    wrote firstrun.sh + hokku/firstboot-install.sh")

    # Patch cmdline.txt — Pi OS expects systemd.run=/boot/firmware/firstrun.sh on a single line
    cmdline_path = boot / "cmdline.txt"
    original = cmdline_path.read_text(encoding="utf-8").rstrip()
    # Remove any prior systemd.run= tokens
    tokens = [t for t in original.split() if not t.startswith("systemd.run") and t != "systemd.run_success_action=reboot"]
    tokens += [
        "systemd.run=/boot/firmware/firstrun.sh",
        "systemd.run_success_action=reboot",
        "systemd.unit=kernel-command-line.target",
    ]
    new_cmdline = " ".join(tokens) + "\n"
    cmdline_path.write_bytes(new_cmdline.encode("utf-8"))
    print("    patched cmdline.txt")


def _shell_escape(s):
    """Single-quote-escape a string for embedding in a bash double-quoted string."""
    return s.replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`").replace('"', '\\"')


def _render_firstrun(cfg):
    """Build the firstrun.sh that runs at initial boot (no network yet)."""
    wifi_ssid = _shell_escape(cfg["wifi_ssid"])
    wifi_pass = _shell_escape(cfg["wifi_pass"])
    user = _shell_escape(cfg["user"])
    password = _shell_escape(cfg["password"])
    ssh = "1" if cfg["ssh_enabled"] else "0"
    samba = "1" if cfg["samba"] else "0"
    hostname = _shell_escape(cfg["hostname"])

    return f"""#!/bin/bash
# Generated by hokku-setup — runs once on first boot.
set +e
exec > /boot/firmware/firstrun.log 2>&1
echo "=== firstrun.sh starting $(date) ==="

CURRENT_HOSTNAME=$(cat /etc/hostname | tr -d ' \\t\\n\\r')
echo "{hostname}" > /etc/hostname
sed -i "s/127\\.0\\.1\\.1.*$CURRENT_HOSTNAME/127.0.1.1\\t{hostname}/g" /etc/hosts

# --- user ---
if ! id -u "{user}" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "{user}"
fi
echo "{user}:{password}" | chpasswd
usermod -aG sudo,netdev,gpio,spi,i2c,dialout,video,audio,plugdev "{user}" 2>/dev/null || true

# Remove the default 'pi' user if it exists and isn't our chosen user
if [ "{user}" != "pi" ] && id -u pi >/dev/null 2>&1; then
    deluser --remove-home pi 2>/dev/null || true
fi

# Disable raspi-config's own first-boot prompts
rm -f /etc/ssh/sshd_config.d/rename_user.conf /etc/profile.d/userconfig.sh 2>/dev/null
[ -f /etc/systemd/system/userconfig.service ] && systemctl disable userconfig.service

# --- ssh ---
if [ "{ssh}" = "1" ]; then
    systemctl enable ssh
else
    systemctl disable ssh
fi

# --- wifi via NetworkManager (Bookworm default) ---
mkdir -p /etc/NetworkManager/system-connections
UUID=$(cat /proc/sys/kernel/random/uuid)
cat > /etc/NetworkManager/system-connections/preconfigured.nmconnection <<EOF
[connection]
id=preconfigured
uuid=$UUID
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid={cfg["wifi_ssid"]}

[wifi-security]
key-mgmt=wpa-psk
psk={cfg["wifi_pass"]}

[ipv4]
method=auto

[ipv6]
method=auto
EOF
chmod 600 /etc/NetworkManager/system-connections/preconfigured.nmconnection

# Also write wpa_supplicant as fallback for older OS variants
cat > /etc/wpa_supplicant/wpa_supplicant.conf <<EOF
country=GB
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={{
    ssid="{wifi_ssid}"
    psk="{wifi_pass}"
    key_mgmt=WPA-PSK
}}
EOF
chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf

# --- avahi (mDNS) ---
systemctl enable avahi-daemon 2>/dev/null || true

# --- expand rootfs ---
raspi-config --expand-rootfs 2>/dev/null || true

# --- install second-boot service for .deb + optional samba ---
cat > /etc/systemd/system/hokku-firstboot.service <<'EOF'
[Unit]
Description=Install hokku-server on first boot
After=network-online.target
Wants=network-online.target
ConditionPathExists=/boot/firmware/hokku/firstboot-install.sh

[Service]
Type=oneshot
ExecStart=/bin/bash /boot/firmware/hokku/firstboot-install.sh
RemainAfterExit=yes
StandardOutput=append:/var/log/hokku-firstboot.log
StandardError=append:/var/log/hokku-firstboot.log

[Install]
WantedBy=multi-user.target
EOF
chmod 644 /etc/systemd/system/hokku-firstboot.service
systemctl enable hokku-firstboot.service

[ "{samba}" = "1" ] && touch /boot/firmware/hokku/install-samba

# --- clean up cmdline.txt ---
sed -i 's| systemd\\.run[^ ]*||g' /boot/firmware/cmdline.txt
rm -f /boot/firmware/firstrun.sh

echo "=== firstrun.sh done $(date) ==="
exit 0
"""


def _render_firstboot(cfg):
    """Build firstboot-install.sh — runs on the first reboot, after network is up."""
    user = _shell_escape(cfg["user"])
    password = _shell_escape(cfg["password"])
    samba_share = "home"  # samba share path label

    return f"""#!/bin/bash
# Generated by hokku-setup — runs after first reboot once network is up.
set -e
exec > /var/log/hokku-firstboot-install.log 2>&1
echo "=== firstboot-install.sh starting $(date) ==="

export DEBIAN_FRONTEND=noninteractive

# Wait up to 60s for apt to be usable (avoids racing with unattended-upgrades)
for i in $(seq 1 60); do
    if ! pgrep -f 'apt-get|dpkg|unattended-upgrade' >/dev/null; then
        break
    fi
    sleep 1
done

apt-get update || true

DEB=/boot/firmware/hokku/hokku-server.deb
if [ -f "$DEB" ]; then
    apt-get install -y "$DEB"
    systemctl enable hokku-server
    systemctl start hokku-server
fi

if [ -f /boot/firmware/hokku/install-samba ]; then
    apt-get install -y samba
    # Add the user to samba with plaintext pw
    (echo "{password}"; echo "{password}") | smbpasswd -a -s "{user}"
    cat >> /etc/samba/smb.conf <<'SMBEOF'

[{samba_share}]
   comment = Hokku home share
   path = /home/{cfg["user"]}
   valid users = {cfg["user"]}
   read only = no
   browseable = yes
   create mask = 0644
   directory mask = 0755
SMBEOF
    systemctl restart smbd
    systemctl enable smbd
fi

# Disable self
systemctl disable hokku-firstboot.service
rm -f /etc/systemd/system/hokku-firstboot.service
rm -rf /boot/firmware/hokku
systemctl daemon-reload

echo "=== firstboot-install.sh done $(date) ==="
"""


# ---------- Wait for Pi on network ----------

def wait_for_mdns(hostname):
    """Poll for hostname.local resolution. No timeout. Ctrl-C to cancel."""
    fqdn = f"{hostname}.local"
    print()
    print(f"  Waiting for {fqdn} on the network...")
    print("  (no timeout — Ctrl-C to cancel; first boot can take several minutes)")
    spin = "|/-\\"
    i = 0
    start = time.time()
    while True:
        try:
            ip = socket.gethostbyname(fqdn)
            print(f"\n  Found {fqdn} at {ip} (after {int(time.time() - start)}s).")
            return ip
        except socket.gaierror:
            pass
        elapsed = int(time.time() - start)
        sys.stdout.write(f"\r  waiting {spin[i % 4]}  [{elapsed}s elapsed] ")
        sys.stdout.flush()
        i += 1
        time.sleep(2)


def wait_for_webserver(hostname, port=WEBSERVER_PORT, timeout=WEBSERVER_WAIT_SECS):
    """Poll HTTP /hokku/api/time. Returns True if reachable within timeout."""
    url = f"http://{hostname}.local:{port}/hokku/api/time"
    print(f"  Waiting up to {timeout}s for webserver at {url}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    print(f"  Webserver OK (after {int(timeout - (deadline - time.time()))}s).")
                    return True
        except Exception:
            pass
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(2)
    print()
    print("  TIMEOUT: webserver did not respond.")
    print("  The .deb install may still be running. SSH in and check /var/log/hokku-firstboot-install.log.")
    return False


def check_existing_server(hostname=PI_OS_HOSTNAME):
    """Quick probe without waiting. Returns True if a hokku-server is already running."""
    fqdn = f"{hostname}.local"
    print(f"  Probing {fqdn}...", end=" ", flush=True)
    try:
        ip = socket.gethostbyname(fqdn)
    except socket.gaierror:
        print("not found on mDNS.")
        return False
    print(f"resolved to {ip}.")
    try:
        with urllib.request.urlopen(f"http://{fqdn}:{WEBSERVER_PORT}/hokku/api/time", timeout=3) as r:
            if r.status == 200:
                print("  Webserver is running.")
                return True
    except Exception as e:
        print(f"  Webserver not responding: {e}")
    return False


# ---------- Orchestration ----------

def run():
    """Run the full Pi OS install flow. Returns dict with install config
    (wifi_ssid, wifi_pass, server_ip) or None if aborted/failed."""
    if sys.platform != "win32":
        print("  ERROR: Pi installer currently only supports Windows.")
        return None
    if not is_admin():
        print("  ERROR: Administrator privileges are required to write to the SD card.")
        print("  Launch via hokku_setup.bat (it auto-elevates) rather than python directly.")
        return None

    # Pre-flight: .deb must exist before we write anything.
    deb = locate_deb_package_interactive()
    if not deb:
        print("  Aborted: no .deb available.")
        return None
    print(f"  .deb package: {deb.name} ({deb.stat().st_size // 1024} KB)")

    # Step 1 — pick the SD card
    drive = prompt_sd_drive()
    if not drive:
        print("  Aborted: no drive selected.")
        return None

    # Step 2 — image
    image = prompt_image_path()
    if not image:
        return None

    # Step 3 — settings + confirm wipe + write + customize
    print()
    print("  Step 3 of 4: Write image and configure")
    print("  --------------------------------------")
    cfg = collect_install_config()

    letters = list_drive_letters_for(drive["index"])
    print()
    print("  !! ALL DATA ON THE FOLLOWING DEVICE WILL BE ERASED !!")
    print(f"    PhysicalDrive{drive['index']}  {drive['model']}  "
          f"{fmt_gb(drive['size_bytes'])}  [{', '.join(letters) or 'no drive letter'}]")
    if input("  Type 'YES' to proceed: ").strip() != "YES":
        print("  Aborted.")
        return None

    if not write_image_to_disk(image, drive["index"], drive["size_bytes"]):
        return None

    print("  Locating bootfs partition...")
    boot_letter = find_bootfs_letter(drive["index"])
    if not boot_letter:
        print("  ERROR: could not find bootfs partition after write. Eject and reinsert card, then manually customize.")
        return None
    print(f"  bootfs at {boot_letter}")
    try:
        inject_boot_customization(boot_letter, cfg, deb)
    except Exception as e:
        print(f"  ERROR during customization: {e}")
        return None

    print()
    print("  SD card ready. Safely eject it before removing.")

    # Step 4 — wait for Pi
    print()
    print("  Step 4 of 4: Wait for Pi to come online")
    print("  --------------------------------------")
    print("  Insert the SD card into the Pi Zero W2 and power it on.")
    print()
    print("  !! FIRST BOOT IS SLOW !!")
    print("     - Boot 1: customizes the OS, then reboots (~1-2 min)")
    print("     - Boot 2: runs apt update + installs the .deb (+samba, if chosen)")
    print("     - Total first-boot time: typically 3-8 minutes,")
    print("       longer on slow SD cards or slow internet.")
    print("     - If something seems stuck, SSH in and tail:")
    print("         /var/log/hokku-firstboot-install.log")
    print("         /boot/firmware/firstrun.log")
    try:
        pi_ip = wait_for_mdns(cfg["hostname"])
    except KeyboardInterrupt:
        print("\n  Cancelled by user.")
        return None

    webserver_ok = wait_for_webserver(cfg["hostname"])

    return {
        "wifi_ssid": cfg["wifi_ssid"],
        "wifi_pass": cfg["wifi_pass"],
        "server_ip": pi_ip,
        "hostname": cfg["hostname"],
        "webserver_ok": webserver_ok,
    }
