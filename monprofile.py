#!/usr/bin/env python3
"""
monprofile - save and restore Windows monitor configurations from the command line.

Uses the Windows CCD API (QueryDisplayConfig / SetDisplayConfig), the same API
Windows' own display settings use. Works with the "virtual mode aware" and
"virtual refresh rate aware" flags, which is what carries Dynamic Refresh Rate
(adaptive refresh) information on current Windows 11 builds.

Pure standard library, no pip installs needed.

Usage:
    python monprofile.py save  <name>      save the current configuration
    python monprofile.py load  <name>      apply a saved configuration
    python monprofile.py list              list saved profiles
    python monprofile.py show  [name]      show current config (or a saved profile)
    python monprofile.py delete <name>     delete a profile
    python monprofile.py --dir D ...       use another profile folder

Profiles live in %APPDATA%\\monprofile\\<name>.json
"""
import argparse
import ctypes
import json
import os
import re
import sys
import time
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_int32,
    c_uint16,
    c_uint32,
    c_uint64,
    c_wchar,
    sizeof,
)

if os.name != "nt":
    sys.exit("This tool only works on Windows.")

UINT32, UINT16, UINT64, LONG = c_uint32, c_uint16, c_uint64, c_int32
user32 = ctypes.WinDLL("user32", use_last_error=True)

# ----------------------------------------------------------------- constants
QDC_ALL_PATHS = 0x1
QDC_ONLY_ACTIVE_PATHS = 0x2
QDC_VIRTUAL_MODE_AWARE = 0x10
QDC_VIRTUAL_REFRESH_RATE_AWARE = 0x40

SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x20
SDC_APPLY = 0x80
SDC_NO_OPTIMIZATION = 0x100
SDC_SAVE_TO_DATABASE = 0x200
SDC_ALLOW_CHANGES = 0x400
SDC_VIRTUAL_MODE_AWARE = 0x8000
SDC_VIRTUAL_REFRESH_RATE_AWARE = 0x20000

PATH_ACTIVE = 0x1
INFO_SOURCE, INFO_TARGET, INFO_DESKTOP = 1, 2, 3

DEVINFO_GET_TARGET_NAME = 2
DEVINFO_GET_ADAPTER_NAME = 4
DEVINFO_GET_ADVANCED_COLOR_INFO = 9
DEVINFO_SET_ADVANCED_COLOR_STATE = 10

ERROR_INSUFFICIENT_BUFFER = 122
ROTATION = {1: "0", 2: "90", 3: "180", 4: "270"}


# ----------------------------------------------------------------- structures
class LUID(Structure):
    _fields_ = [("LowPart", UINT32), ("HighPart", LONG)]


class RATIONAL(Structure):
    _fields_ = [("Numerator", UINT32), ("Denominator", UINT32)]


class REGION(Structure):
    _fields_ = [("cx", UINT32), ("cy", UINT32)]


class POINTL(Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECTL(Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class SOURCE_INFO(Structure):
    _fields_ = [("adapterId", LUID), ("id", UINT32),
                ("modeInfoIdx", UINT32), ("statusFlags", UINT32)]


class TARGET_INFO(Structure):
    _fields_ = [("adapterId", LUID), ("id", UINT32), ("modeInfoIdx", UINT32),
                ("outputTechnology", UINT32), ("rotation", UINT32),
                ("scaling", UINT32), ("refreshRate", RATIONAL),
                ("scanLineOrdering", UINT32), ("targetAvailable", LONG),
                ("statusFlags", UINT32)]


class PATH_INFO(Structure):
    _fields_ = [("sourceInfo", SOURCE_INFO), ("targetInfo", TARGET_INFO),
                ("flags", UINT32)]


class VIDEO_SIGNAL_INFO(Structure):
    _fields_ = [("pixelRate", UINT64), ("hSyncFreq", RATIONAL),
                ("vSyncFreq", RATIONAL), ("activeSize", REGION),
                ("totalSize", REGION),
                ("videoStandard", UINT32),  # low 16: standard, bits 16-21: vSyncFreqDivider
                ("scanLineOrdering", UINT32)]


class TARGET_MODE(Structure):
    _fields_ = [("targetVideoSignalInfo", VIDEO_SIGNAL_INFO)]


class SOURCE_MODE(Structure):
    _fields_ = [("width", UINT32), ("height", UINT32),
                ("pixelFormat", UINT32), ("position", POINTL)]


class DESKTOP_IMAGE_INFO(Structure):
    _fields_ = [("PathSourceSize", POINTL), ("DesktopImageRegion", RECTL),
                ("DesktopImageClip", RECTL)]


class MODE_UNION(Union):
    _fields_ = [("targetMode", TARGET_MODE), ("sourceMode", SOURCE_MODE),
                ("desktopImageInfo", DESKTOP_IMAGE_INFO)]


class MODE_INFO(Structure):
    _fields_ = [("infoType", UINT32), ("id", UINT32),
                ("adapterId", LUID), ("mode", MODE_UNION)]


class HEADER(Structure):
    _fields_ = [("type", UINT32), ("size", UINT32),
                ("adapterId", LUID), ("id", UINT32)]


class ADAPTER_NAME(Structure):
    _fields_ = [("header", HEADER), ("adapterDevicePath", c_wchar * 128)]


class TARGET_NAME(Structure):
    _fields_ = [("header", HEADER), ("flags", UINT32),
                ("outputTechnology", UINT32), ("edidManufactureId", UINT16),
                ("edidProductCodeId", UINT16), ("connectorInstance", UINT32),
                ("monitorFriendlyDeviceName", c_wchar * 64),
                ("monitorDevicePath", c_wchar * 128)]


class ADV_COLOR_INFO(Structure):
    _fields_ = [("header", HEADER), ("value", UINT32),
                ("colorEncoding", UINT32), ("bitsPerColorChannel", UINT32)]


class ADV_COLOR_SET(Structure):
    _fields_ = [("header", HEADER), ("value", UINT32)]


# Sanity check against the documented Win32 sizes.
assert sizeof(PATH_INFO) == 72 and sizeof(MODE_INFO) == 64, "struct layout mismatch"

user32.GetDisplayConfigBufferSizes.argtypes = [UINT32, POINTER(UINT32), POINTER(UINT32)]
user32.QueryDisplayConfig.argtypes = [UINT32, POINTER(UINT32), POINTER(PATH_INFO),
                                      POINTER(UINT32), POINTER(MODE_INFO),
                                      ctypes.c_void_p]
user32.SetDisplayConfig.argtypes = [UINT32, POINTER(PATH_INFO), UINT32,
                                    POINTER(MODE_INFO), UINT32]


# ----------------------------------------------------------------- helpers
class DisplayError(Exception):
    pass


def to_hex(s):
    return bytes(s).hex()


def from_hex(cls, h):
    return cls.from_buffer_copy(bytes.fromhex(h))


def luid_key(l):
    return (l.LowPart, l.HighPart)


def make_luid(key):
    l = LUID()
    l.LowPart, l.HighPart = key
    return l


def query(flags):
    """Return (paths, n_paths, modes, n_modes). Raises DisplayError on failure."""
    for _ in range(10):
        np_, nm = UINT32(), UINT32()
        rc = user32.GetDisplayConfigBufferSizes(flags, byref(np_), byref(nm))
        if rc:
            raise DisplayError(rc)
        paths = (PATH_INFO * max(np_.value, 1))()
        modes = (MODE_INFO * max(nm.value, 1))()
        rc = user32.QueryDisplayConfig(flags, byref(np_), paths, byref(nm), modes, None)
        if rc == ERROR_INSUFFICIENT_BUFFER:
            continue
        if rc:
            raise DisplayError(rc)
        return paths, np_.value, modes, nm.value
    raise DisplayError(ERROR_INSUFFICIENT_BUFFER)


def query_active():
    """Query active config with the most capable awareness flags this Windows accepts."""
    last = None
    for aware in (QDC_VIRTUAL_MODE_AWARE | QDC_VIRTUAL_REFRESH_RATE_AWARE,
                  QDC_VIRTUAL_MODE_AWARE, 0):
        flags = QDC_ONLY_ACTIVE_PATHS | aware
        try:
            return (flags,) + query(flags)
        except DisplayError as e:
            last = e
    raise last


_adapter_cache = {}


def adapter_path(luid):
    k = luid_key(luid)
    if k not in _adapter_cache:
        n = ADAPTER_NAME()
        n.header.type = DEVINFO_GET_ADAPTER_NAME
        n.header.size = sizeof(n)
        n.header.adapterId = luid
        rc = user32.DisplayConfigGetDeviceInfo(byref(n))
        _adapter_cache[k] = n.adapterDevicePath if rc == 0 else None
    return _adapter_cache[k]


def target_name(luid, tid):
    n = TARGET_NAME()
    n.header.type = DEVINFO_GET_TARGET_NAME
    n.header.size = sizeof(n)
    n.header.adapterId = luid
    n.header.id = tid
    if user32.DisplayConfigGetDeviceInfo(byref(n)) != 0:
        return "", ""
    return n.monitorFriendlyDeviceName, n.monitorDevicePath


def get_hdr(luid, tid):
    """Return (supported, enabled) or (False, False)."""
    i = ADV_COLOR_INFO()
    i.header.type = DEVINFO_GET_ADVANCED_COLOR_INFO
    i.header.size = sizeof(i)
    i.header.adapterId = luid
    i.header.id = tid
    if user32.DisplayConfigGetDeviceInfo(byref(i)) != 0:
        return False, False
    return bool(i.value & 1), bool(i.value & 2)


def set_hdr(luid, tid, enable):
    s = ADV_COLOR_SET()
    s.header.type = DEVINFO_SET_ADVANCED_COLOR_STATE
    s.header.size = sizeof(s)
    s.header.adapterId = luid
    s.header.id = tid
    s.value = 1 if enable else 0
    return user32.DisplayConfigSetDeviceInfo(byref(s)) == 0


def summarize(paths, np_, modes, nm):
    """Human-readable view of the active config (also used for verification)."""
    idx = {(luid_key(modes[i].adapterId), modes[i].id, modes[i].infoType): modes[i]
           for i in range(nm)}
    out = []
    for i in range(np_):
        p = paths[i]
        if not (p.flags & PATH_ACTIVE):
            continue
        t, s = p.targetInfo, p.sourceInfo
        tm = idx.get((luid_key(t.adapterId), t.id, INFO_TARGET))
        sm = idx.get((luid_key(s.adapterId), s.id, INFO_SOURCE))
        name, _ = target_name(t.adapterId, t.id)
        e = {"adapter": adapter_path(t.adapterId), "target_id": t.id,
             "name": name, "rotation": t.rotation,
             "width": 0, "height": 0, "x": 0, "y": 0,
             "hz": 0.0, "divider": 1, "virtual_hz": 0.0}
        if sm:
            m = sm.mode.sourceMode
            e.update(width=m.width, height=m.height,
                     x=m.position.x, y=m.position.y)
        if tm:
            v = tm.mode.targetMode.targetVideoSignalInfo
            n, d = v.vSyncFreq.Numerator, v.vSyncFreq.Denominator
            hz = n / d if d else 0.0
            div = max((v.videoStandard >> 16) & 0x3F, 1)
            e.update(hz=round(hz, 3), divider=div, virtual_hz=round(hz / div, 3))
        sup, en = get_hdr(t.adapterId, t.id)
        e["hdr_supported"], e["hdr"] = sup, en
        out.append(e)
    return out


def print_summary(entries):
    for e in entries:
        print(f"  {e['name'] or '(unknown monitor)'}  [target {e['target_id']}]  "
              f"{e['width']}x{e['height']} at ({e['x']},{e['y']})  "
              f"rotation {ROTATION.get(e['rotation'], e['rotation'])}")
        line = (f"      vsync {e['hz']:.3f} Hz, divider {e['divider']}, "
                f"effective {e['virtual_hz']:.3f} Hz")
        if e.get("hdr_supported"):
            line += f", HDR {'on' if e['hdr'] else 'off'}"
        print(line)


# ----------------------------------------------------------------- profiles
def profile_dir(override):
    d = override or os.path.join(os.environ.get("APPDATA", "."), "monprofile")
    os.makedirs(d, exist_ok=True)
    return d


def profile_file(a, name):
    if not re.fullmatch(r"[\w .\-]+", name):
        sys.exit("Profile names may only contain letters, digits, space, _ . -")
    return os.path.join(profile_dir(a.dir), name + ".json")


def cmd_save(a):
    flags, paths, np_, modes, nm = query_active()
    adapters = {}
    for i in range(np_):
        for l in (paths[i].sourceInfo.adapterId, paths[i].targetInfo.adapterId):
            adapters[luid_key(l)] = adapter_path(l)
    for i in range(nm):
        adapters[luid_key(modes[i].adapterId)] = adapter_path(modes[i].adapterId)
    summary = summarize(paths, np_, modes, nm)
    prof = {
        "version": 1,
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        "query_flags": flags,
        "adapters": [{"luid": list(k), "path": v} for k, v in adapters.items()],
        "paths": [to_hex(paths[i]) for i in range(np_)],
        "modes": [to_hex(modes[i]) for i in range(nm)],
        "summary": summary,
    }
    with open(profile_file(a, a.name), "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=2)
    print(f"Saved profile '{a.name}' ({len(summary)} active display(s)):")
    print_summary(summary)
    if not flags & QDC_VIRTUAL_REFRESH_RATE_AWARE:
        print("\nNote: this Windows build did not accept the virtual-refresh-rate flag; "
              "saved without it.")


def current_adapter_map():
    """adapter device path -> current LUID key (all adapters incl. inactive outputs)."""
    result = {}
    paths, np_, _, _ = query(QDC_ALL_PATHS)
    for i in range(np_):
        for l in (paths[i].sourceInfo.adapterId, paths[i].targetInfo.adapterId):
            p = adapter_path(l)
            if p:
                result[p] = luid_key(l)
    return result


def cmd_load(a):
    fn = profile_file(a, a.name)
    if not os.path.exists(fn):
        sys.exit(f"No profile named '{a.name}'. Use 'list' to see saved profiles.")
    with open(fn, encoding="utf-8") as f:
        prof = json.load(f)

    # LUIDs change between reboots, so remap saved adapters via their device path.
    cur = current_adapter_map()
    remap = {}
    for ad in prof["adapters"]:
        old = tuple(ad["luid"])
        if ad["path"] not in cur:
            sys.exit("A graphics adapter used by this profile is not present anymore:\n  "
                     + str(ad["path"]))
        remap[old] = cur[ad["path"]]

    paths = [from_hex(PATH_INFO, h) for h in prof["paths"]]
    modes = [from_hex(MODE_INFO, h) for h in prof["modes"]]
    for p in paths:
        p.sourceInfo.adapterId = make_luid(remap[luid_key(p.sourceInfo.adapterId)])
        p.targetInfo.adapterId = make_luid(remap[luid_key(p.targetInfo.adapterId)])
    for m in modes:
        m.adapterId = make_luid(remap[luid_key(m.adapterId)])
    parr = (PATH_INFO * max(len(paths), 1))(*paths)
    marr = (MODE_INFO * max(len(modes), 1))(*modes)

    qf = prof["query_flags"]
    aware = 0
    if qf & QDC_VIRTUAL_MODE_AWARE:
        aware |= SDC_VIRTUAL_MODE_AWARE
    if qf & QDC_VIRTUAL_REFRESH_RATE_AWARE:
        aware |= SDC_VIRTUAL_REFRESH_RATE_AWARE
    base = SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_SAVE_TO_DATABASE | aware

    rc = None
    for extra in (SDC_NO_OPTIMIZATION, SDC_ALLOW_CHANGES):
        rc = user32.SetDisplayConfig(len(paths), parr, len(modes), marr, base | extra)
        if rc == 0:
            break
    if rc != 0:
        sys.exit(f"SetDisplayConfig failed with error {rc} "
                 f"({ctypes.FormatError(rc).strip()}).")
    print(f"Applied profile '{a.name}'.")

    time.sleep(1.5)

    # HDR ("advanced color") is a separate per-monitor switch; restore it too.
    if not a.no_hdr:
        for e in prof["summary"]:
            if not e.get("hdr_supported") or e["adapter"] not in cur:
                continue
            luid = make_luid(cur[e["adapter"]])
            sup, en = get_hdr(luid, e["target_id"])
            if sup and en != e["hdr"]:
                set_hdr(luid, e["target_id"], e["hdr"])
        time.sleep(1.0)

    if a.no_verify:
        return
    _, p2, np2, m2, nm2 = query_active()
    now = {(e["adapter"], e["target_id"]): e for e in summarize(p2, np2, m2, nm2)}
    keys = ("width", "height", "x", "y", "rotation", "hz", "divider")
    problems = 0
    for e in prof["summary"]:
        got = now.get((e["adapter"], e["target_id"]))
        label = e["name"] or f"target {e['target_id']}"
        if got is None:
            print(f"  MISMATCH {label}: display is not active")
            problems += 1
            continue
        diffs = [f"{k}: wanted {e[k]}, got {got[k]}" for k in keys if e[k] != got[k]]
        if e.get("hdr_supported") and e["hdr"] != got["hdr"]:
            diffs.append(f"HDR: wanted {e['hdr']}, got {got['hdr']}")
        if diffs:
            problems += 1
            print(f"  MISMATCH {label}: " + "; ".join(diffs))
    print("Verified: current state matches the profile." if not problems
          else "Some settings differ from the saved profile (see above).")


def cmd_list(a):
    d = profile_dir(a.dir)
    names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
    if not names:
        print(f"No profiles in {d}")
    for n in names:
        try:
            with open(os.path.join(d, n + ".json"), encoding="utf-8") as f:
                p = json.load(f)
            print(f"{n}  ({len(p['summary'])} display(s), saved {p['saved']})")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            print(f"{n}  (unreadable)")


def cmd_show(a):
    if a.name:
        fn = profile_file(a, a.name)
        if not os.path.exists(fn):
            sys.exit(f"No profile named '{a.name}'.")
        with open(fn, encoding="utf-8") as f:
            print(f"Profile '{a.name}':")
            print_summary(json.load(f)["summary"])
    else:
        _, paths, np_, modes, nm = query_active()
        print("Current configuration:")
        print_summary(summarize(paths, np_, modes, nm))


def cmd_delete(a):
    fn = profile_file(a, a.name)
    if not os.path.exists(fn):
        sys.exit(f"No profile named '{a.name}'.")
    os.remove(fn)
    print(f"Deleted '{a.name}'.")


def main():
    ap = argparse.ArgumentParser(description="Save/restore Windows monitor configurations.")
    ap.add_argument("--dir", help="profile folder (default %%APPDATA%%\\monprofile)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c, fn in (("save", cmd_save), ("delete", cmd_delete)):
        s = sub.add_parser(c)
        s.add_argument("name")
        s.set_defaults(func=fn)
    s = sub.add_parser("load")
    s.add_argument("name")
    s.add_argument("--no-hdr", action="store_true", help="don't restore HDR state")
    s.add_argument("--no-verify", action="store_true", help="skip the post-apply check")
    s.set_defaults(func=cmd_load)
    sub.add_parser("list").set_defaults(func=cmd_list)
    s = sub.add_parser("show")
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_show)
    a = ap.parse_args()
    try:
        a.func(a)
    except DisplayError as e:
        sys.exit(f"Windows display API error {e.args[0]}: "
                 f"{ctypes.FormatError(e.args[0]).strip()}")


if __name__ == "__main__":
    main()