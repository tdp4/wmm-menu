# WMM OBS Launcher (`wmm-menu`)

`wmm-menu` is a Windows GUI launcher for managing and starting multiple portable **OBS Studio** installations. It replaces batch scripts (`start-obs.cmd`) and legacy menu launchers with a modern light/dark-aware interface, locale-independent ZIP configuration backups, and automatic sync service control.

This was written out of necessity to control starting multiple portable instances of OBS, create backups of the configuration on each run, and let you know if OBS crashed on exit.  It also incorporates detection of Syncthing and Resilio Sync's services, assumes if either exists they would be used to synchronize your setup to an external location and will stop them if they exist, and start them back up once OBS is stopped.  Ideally the resources use may be needed to use OBS effectively.

This has been written in python, and should be compiled using the included `build.bat` file.  The compiled version in `build` can then be copied to your OBS machine.

---

## Features

- **Portable OBS Profiles**: Displays equal-width buttons for each configured OBS folder and automatically shows the detected `obs64.exe` product version (e.g., `[32.1.2]`).
- **Fast Launch Option**: Each button includes an attached yellow lightning bolt button (**Start fast without a backup**) to skip the backup step when needed.
- **Locale-Independent Config Backups**: Archives `<OBS Folder>\config` into `<Backup Folder>\<OBS Folder>-YYYYMMDD_HHMMSS.zip` (24-hour clock) with live progress before starting OBS. Excludes volatile browser caches, logs, and crash dumps by default for fast backups.
- **Automatic Sync Pause & Resume**:
  - Monitors Windows services **`syncthing`** and **`rslsyncsvc`** (Resilio Sync).
  - Automatically detects user-installed **SyncTrayzor** and controls it in place of the `syncthing` Windows service when present.
  - Stops running sync services before backing up/starting OBS, and restarts them only after the **last** launched OBS instance closes.
  - Click any **Running** or **Stopped** status pill at the bottom of the menu to toggle it manually.
- **Concurrent Non-Blocking Launches**: Each OBS instance runs in its own background thread. Active buttons are disabled while running so the same OBS folder cannot be started twice, while allowing other OBS instances to be started simultaneously.
- **Modern Adaptive UI**: Follows Windows System Light/Dark mode automatically, centers on the primary monitor at startup, and dynamically resizes its height to fit your configured buttons.

---

## Using the Application

### Running the Compiled Executable (Target Machines)
The compiled `dist\wmm-menu.exe` is a standalone Windows 10/11 (64-bit) executable — **Python does not need to be installed on target machines** to run `wmm-menu.exe`.

```powershell
# Load default menu.yaml located in the same folder as wmm-menu.exe
.\wmm-menu.exe

# Load a custom YAML configuration file (positional or --config / -c)
.\wmm-menu.exe AcoustiCafe.yaml
.\wmm-menu.exe --config C:\obs-studio\WesMasonMedia.yaml
```

> **Tip**: Create Windows shortcuts (`.lnk`) pointing to `wmm-menu.exe` with different `.yaml` filenames to maintain separate menus (for example, `AcoustiCafe.yaml` and `WesMasonMedia.yaml`).

### Main Menu Controls
- **Main Button (`Name [Version]`)**: Stops sync services, backs up the OBS `config` folder to a timestamped `.zip`, and launches `bin\64bit\obs64.exe --portable --disable-updater`.
- **Lightning Bolt Button (`⚡`)**: Starts OBS immediately without creating a config backup (stops/restarts sync services as normal).
- **Right-Click a Button**: Quickly **Edit...** or **Remove** an item when it is not currently running.
- **Gear Button (top-right)**: Opens the **Configuration** dialog.
- **Services Bar (bottom)**: Displays `Running`, `Stopped`, or `N/A` for `syncthing` (or `SyncTrayzor`) and `rslsyncsvc`. Click a `Running` or `Stopped` pill to toggle it.

### What Happens During an OBS Launch
1. A status dialog opens in the **top-left** corner of the primary screen.
2. Any running `syncthing` / `SyncTrayzor` or `rslsyncsvc` service is stopped.
3. Unless started with the lightning bolt button, `<Base Folder>\<Subfolder>\config` is compressed to `<Backup Folder>\<Subfolder>-YYYYMMDD_HHMMSS.zip` with a progress bar. If a backup error occurs, you are prompted (**Yes/No**) on whether to continue launching OBS.
4. OBS starts in portable mode (`--portable --disable-updater`) and the status dialog hides automatically while OBS is open.
5. When OBS exits:
   - If OBS returns a non-zero error code, an error dialog displays the exit code and error output.
   - Once no other launched OBS instances remain open, any sync services that were stopped by the launcher are restarted automatically.

### Configuration Dialog (Gear Icon)
- **Title**: Sets the window title bar and header text for the current YAML config.
- **Base Folder**: Root directory containing your portable OBS subfolders (defaults to `C:\obs-studio`).
- **Backup Folder**: Destination folder for `.zip` config backups (defaults to `<Base Folder>\backups`).
- **Exclude volatile caches & logs**: Skips `obs-browser` caches, `logs`, `crashes`, `profiler_data`, and `updates` during backup (enabled by default).
- **Run this menu automatically when user logs in**: Registers/removes a per-user Windows login entry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`) pointing to `wmm-menu` with `--config <your-yaml-path>`.
- **Menu Buttons**:
  - Click **+ Add** to add a new OBS button (auto-detects subfolders under Base Folder containing `bin\64bit\obs64.exe`).
  - Click **+ Divider** to insert a horizontal divider line between groups of buttons.
  - Use **`▲`** / **`▼`** to reorder buttons and dividers together, **Edit** to rename or change subfolders, or **`✕`** to remove an item.
  - Click **Save** to write changes to the YAML file and resize the menu automatically.
- **Grant Start/Stop Service Permissions (Admin)**: Prompts once for Administrator elevation (UAC) and updates the Windows service ACL (`sc.exe sdset`) for installed `syncthing` / `rslsyncsvc` services so the current Windows user can start and stop them without UAC prompts during normal use. *(SyncTrayzor runs in user space and does not require Admin permissions.)*

### Example `menu.yaml`
If the specified YAML file does not exist on startup, it is created automatically with default settings:

```yaml
title: WMM OBS Launcher
base_folder: C:\obs-studio
backup_folder: C:\obs-studio\backups
backup_exclude_caches: true
items:
  - name: Funhouse
    subfolder: OBS-Acousticafe-32.1-funhouse
  - type: divider
  - name: WMM Special 32
    subfolder: OBS-WMM-Special-32
```

---

## Running from Source & Building the `.exe`

### Prerequisites
- **Operating System**: Windows 10 or Windows 11 (64-bit)
- **Python**: **Python 3.10 or newer** (64-bit) is required to run `wmm-menu.py` from source or compile `wmm-menu.exe`.
  - Download from [python.org/downloads/windows](https://www.python.org/downloads/windows/).
  - During installation, ensure **`tcl/tk and IDLE`** (`tkinter`, enabled by default) and **`pip`** are selected, and check **"Add python.exe to PATH"**.

> **Why Python 3.10+?** The script uses modern Python type union syntax, `zipfile.ZipFile(..., strict_timestamps=False)`, and current wheels for `customtkinter`, `Pillow`, `PyYAML`, and `PyInstaller`, which require Python 3.10 or newer (tested up through Python 3.14).

### Python Dependencies
Listed in `requirements.txt`:
- `PyYAML>=6.0` — YAML configuration loading/saving
- `customtkinter>=5.2` — Modern system light/dark UI widgets
- `Pillow>=10.0` — HiDPI rendering for the gear and lightning bolt icons
- `pyinstaller>=6.0` — Bundling into a standalone single-file `.exe`

### Running Directly with Python
From `C:\src\wmm-menu`:

```powershell
python -m pip install -r requirements.txt
python wmm-menu.py
```

Or with a specific config file:

```powershell
python wmm-menu.py --config path\to\custom-menu.yaml
```

### Compiling to `wmm-menu.exe`
Run the included `build.bat` script from `C:\src\wmm-menu`:

```bat
build.bat
```

What `build.bat` does:
1. Verifies `python` is available in `PATH`.
2. Installs dependencies from `requirements.txt` automatically if any are missing.
3. Runs PyInstaller (`--onefile --windowed --collect-data customtkinter`) and embeds `app.ico` (if present in `C:\src\wmm-menu`) as both the `.exe` file icon and window icon.
4. Outputs the standalone executable to:
   ```text
   C:\src\wmm-menu\dist\wmm-menu.exe
   ```

To deploy to another Windows 10/11 PC, simply copy `dist\wmm-menu.exe` (and optionally your `menu.yaml`) to any folder on that machine.
