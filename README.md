# FLUX
**File-Link-Universal-Exchange**

### Hardware-Adaptive Nanosecond Pipeline
- **Nanosecond & Hardware Detection**: Detects CPU architecture, core concurrency, and system clock on boot in sub-milliseconds.
- **Dynamic Parallel Channels**: Starts with active channels and dynamically expands up to 4 parallel lanes based on throughput and idle lane availability.
- **Explicit Receiver Folders**: Allows safe download folder selection directly in the UI (Default `/uploads`, `Downloads`, `Desktop`, `Documents`, `Pictures`, `Videos`). Strict security boundary prevents directory traversal or arbitrary root/system drive writing.
- **Incoming Transfer Indicator**: Displays a dedicated incoming stream notification window without flashing console windows (`--windows-hide-console`).

### Quick Start
- **Windows**: Run `FLUX_V2.exe` or `start.bat`
- **Linux**: `./dist/flux-linux-x64` or `./dist/flux-linux-arm64`
- **macOS**: `./dist/flux-macos-arm64` (Apple Silicon) or `./dist/flux-macos-x64` (Intel)

Web UI runs at `http://localhost:1000`.
