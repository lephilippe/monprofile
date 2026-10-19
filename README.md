# monprofile

Save your Windows monitor setup to a named profile and switch back to it from the command line.

I used MonitorProfileSwitcher for this until a Windows update added adaptive refresh rate and it stopped working. This does the same job and handles the newer display flags.

It saves resolution, refresh rate (including dynamic refresh rate info), position, rotation, which monitors are active, which one is primary, and HDR on/off per monitor.

## Requirements

- Windows 10/11
- Python 3.8+ (no packages needed)

## Usage

```
python monprofile.py save <name>     # save the current setup
python monprofile.py load <name>     # apply a saved setup
python monprofile.py list            # list profiles
python monprofile.py show [name]     # print current setup, or a saved profile
python monprofile.py delete <name>   # delete profile
```

Profiles are stored as JSON in `%APPDATA%\monprofile`. Use `--dir <folder>` to put them somewhere else.

`load` re-reads the display state afterwards and tells you if anything doesn't match the profile. Skip that with `--no-verify`, or leave HDR alone with `--no-hdr`.

A typical flow: set your monitors up the way you want in Windows settings, run `save desk`, change things for something else, run `save tv`, and from then on just `load desk` or `load tv`.

## (Optional) Hotkeys with AutoHotkey v2

```autohotkey
#Requires AutoHotkey v2.0

!+Left::Run('cmd /c python "C:\path\to\monprofile.py" load desk',, "Hide")
!+Right::Run('cmd /c python "C:\path\to\monprofile.py" load tv',, "Hide")
```

Alt+Shift+Left and Alt+Shift+Right now switch profiles.

## Notes

- Windows changes adapter IDs on every reboot, so profiles are matched to GPUs by device path instead. Saved profiles keep working after a restart.
- If you swap GPUs or unplug a monitor a profile depends on, loading it will fail or report mismatches.
- Per-monitor DPI scaling isn't saved.
- Only tested on my own setup so far, so issues and PRs are welcome.