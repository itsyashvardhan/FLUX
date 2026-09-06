# FLUX

<p align="center">
  <img src="assets/flux-share-banner.svg" alt="FLUX — fast, friendly file sharing across devices" width="900">
</p>

<p align="center"><strong>Fast, friendly file sharing across devices.</strong><br>Send files over your local network with a clean web interface and native builds for Windows, Linux, and macOS.</p>

<p align="center">
  <a href="https://github.com/itsyashvardhan/FLUX/releases/tag/v0.2">Download v0.2</a> ·
  <a href="https://github.com/itsyashvardhan/FLUX/issues">Report an issue</a>
</p>

## Why FLUX?

- **Simple:** open the app, choose a destination folder, and share.
- **Fast:** adaptive parallel transfer lanes tune themselves to the hardware.
- **Private by default:** files stay on your network instead of passing through a third-party cloud.
- **Cross-platform:** download a ready-to-run build for Windows, Linux, or macOS.
- **Safe receiving:** downloads are constrained to an explicit receiver folder, with traversal protection.

## Release downloads

Get the latest ready-to-run binaries from the [v0.2 release](https://github.com/itsyashvardhan/FLUX/releases/tag/v0.2):

| Platform | Download |
| --- | --- |
| Windows x64 | `flux-windows-x64.exe` |
| Windows x64 · baseline compatibility | `flux-windows-x64-baseline.exe` |
| Linux x64 | `flux-linux-x64` |
| Linux ARM64 | `flux-linux-arm64` |
| macOS Intel | `flux-macos-x64` |
| macOS Apple Silicon | `flux-macos-arm64` |

Each release includes SHA-256 checksums in the release notes.

## Run from source

```bash
bun server.js
```

Then open <http://localhost:1000> in a browser. Shared files are received in the configured receiver folder; the default is `/uploads`.

## Features

- Hardware and CPU architecture detection at startup
- Dynamic parallel transfer channels, up to four lanes
- Clear incoming-transfer indicator without flashing a console window on Windows
- Receiver folders for Downloads, Desktop, Documents, Pictures, Videos, and a custom path
- Windows, Linux, and macOS release builds

## License

See the repository for the current project license and release terms.
