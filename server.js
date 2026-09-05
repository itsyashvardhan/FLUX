import { serve } from "bun";
import { join, dirname, resolve } from "path";
import { mkdir, writeFile, readFile } from "fs/promises";
import { homedir, platform, arch, cpus } from "os";
import { spawn } from "child_process";

const PORT = 1000;
const FLUX_ROOT = (process.env.FLUX_DIR) ? resolve(process.env.FLUX_DIR) : (process.pkg ? dirname(process.execPath) : process.cwd());
const CONFIG_PATH = join(FLUX_ROOT, "flux-config.json");
const DEFAULT_UPLOADS = join(FLUX_ROOT, "uploads");

// Startup hardware timing & detection
const bootNs = Bun.nanoseconds ? Bun.nanoseconds() : Number(process.hrtime.bigint());
const cpuList = cpus() || [];
const detectedHardware = {
  platform: platform(),
  arch: arch(),
  cores: cpuList.length || 1,
  model: cpuList[0]?.model || "Unknown CPU",
  bootNanoseconds: bootNs
};

// Safe allowed destination base directories
const userHome = homedir();
const SAFE_DESTINATIONS = [
  { id: "uploads", label: "FLUX /uploads (Default)", path: DEFAULT_UPLOADS },
  { id: "downloads", label: "Downloads", path: join(userHome, "Downloads") },
  { id: "desktop", label: "Desktop", path: join(userHome, "Desktop") },
  { id: "documents", label: "Documents", path: join(userHome, "Documents") },
  { id: "pictures", label: "Pictures", path: join(userHome, "Pictures") },
  { id: "videos", label: "Videos", path: join(userHome, "Videos") }
];

async function loadConfig() {
  try {
    const raw = await readFile(CONFIG_PATH, "utf8");
    const parsed = JSON.parse(raw);
    const matched = SAFE_DESTINATIONS.find(d => d.id === parsed.destId);
    if (matched) return { destId: matched.id, destPath: matched.path };
  } catch {}
  return { destId: "uploads", destPath: DEFAULT_UPLOADS };
}

let activeConfig = await loadConfig();
await mkdir(activeConfig.destPath, { recursive: true });

function isUnderDirectory(childPath, parentDir) {
  const rel = resolve(childPath);
  const parent = resolve(parentDir);
  return rel === parent || rel.startsWith(parent + (parent.endsWith("\\") || parent.endsWith("/") ? "" : (platform() === "win32" ? "\\" : "/")));
}

function sanitizeDestination(destId) {
  const found = SAFE_DESTINATIONS.find(d => d.id === destId);
  return found ? found : SAFE_DESTINATIONS[0];
}

// Transfer Indicator Popup Window
let popupProcess = null;
let activeTransfersCount = 0;
let popupHideTimer = null;

function showTransferWindow(filename, sizeFormatted) {
  activeTransfersCount++;
  if (popupHideTimer) {
    clearTimeout(popupHideTimer);
    popupHideTimer = null;
  }

  if (popupProcess && !popupProcess.killed) {
    return;
  }

  const osType = platform();
  if (osType === "win32") {
    const psScript = `
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = "FLUX - Incoming Transfer"
$form.Size = New-Object System.Drawing.Size(380, 160)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedToolWindow"
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(17, 24, 39)

$labelTitle = New-Object System.Windows.Forms.Label
$labelTitle.Text = "FLUX: Incoming Transfer"
$labelTitle.Font = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$labelTitle.ForeColor = [System.Drawing.Color]::FromArgb(52, 211, 153)
$labelTitle.Size = New-Object System.Drawing.Size(340, 25)
$labelTitle.Location = New-Object System.Drawing.Point(20, 20)
$form.Controls.Add($labelTitle)

$labelFile = New-Object System.Windows.Forms.Label
$labelFile.Text = "Hardware channels receiving stream..."
$labelFile.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Regular)
$labelFile.ForeColor = [System.Drawing.Color]::FromArgb(148, 163, 184)
$labelFile.Size = New-Object System.Drawing.Size(340, 30)
$labelFile.Location = New-Object System.Drawing.Point(20, 50)
$form.Controls.Add($labelFile)

$prog = New-Object System.Windows.Forms.ProgressBar
$prog.Style = "Marquee"
$prog.MarqueeAnimationSpeed = 30
$prog.Size = New-Object System.Drawing.Size(330, 12)
$prog.Location = New-Object System.Drawing.Point(20, 85)
$form.Controls.Add($prog)

[System.Windows.Forms.Application]::Run($form)
`;
    try {
      popupProcess = spawn("powershell", ["-WindowStyle", "Hidden", "-NoProfile", "-Command", psScript], {
        detached: true,
        stdio: "ignore",
        windowsHide: true
      });
      popupProcess.unref();
    } catch {}
  } else if (osType === "darwin") {
    // macOS native dialog or notification indicator
    try {
      const osaScript = `display notification "Hardware channels receiving stream..." with title "FLUX: Incoming Transfer"`;
      popupProcess = spawn("osascript", ["-e", osaScript], {
        detached: true,
        stdio: "ignore"
      });
      popupProcess.unref();
    } catch {}
  } else if (osType === "linux") {
    // Linux indicator via zenity, kdialog, or notify-send
    try {
      popupProcess = spawn("zenity", ["--info", "--title=FLUX", "--text=Incoming Transfer Stream...", "--timeout=3"], {
        detached: true,
        stdio: "ignore"
      });
      popupProcess.on("error", () => {
        try {
          popupProcess = spawn("notify-send", ["FLUX", "Incoming Transfer Stream..."], { detached: true, stdio: "ignore" });
          popupProcess.unref();
        } catch {}
      });
      popupProcess.unref();
    } catch {}
  }
}

function dismissTransferWindow() {
  activeTransfersCount = Math.max(0, activeTransfersCount - 1);
  if (activeTransfersCount === 0) {
    if (popupHideTimer) clearTimeout(popupHideTimer);
    popupHideTimer = setTimeout(() => {
      if (popupProcess) {
        try {
          popupProcess.kill();
        } catch {}
        popupProcess = null;
      }
    }, 1500);
  }
}

const HTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FLUX</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #090d16;
      color: #f1f5f9;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }
    .card {
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 1rem;
      padding: 2rem;
      width: 100%;
      max-width: 600px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7);
    }
    .header { text-align: center; margin-bottom: 1.25rem; }
    h1 { font-size: 1.75rem; font-weight: 800; letter-spacing: 0.05em; color: #fff; }
    p.sub { color: #64748b; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em; margin-top: 0.25rem; }
    
    .hw-badge {
      display: flex;
      justify-content: space-between;
      background: #0d131f;
      border: 1px solid #1e293b;
      border-radius: 0.5rem;
      padding: 0.5rem 0.75rem;
      margin-bottom: 1.25rem;
      font-size: 0.75rem;
      color: #94a3b8;
    }
    .hw-badge span.chip {
      color: #38bdf8;
      font-weight: 600;
      font-family: monospace;
    }

    .dest-selector {
      background: #0d131f;
      border: 1px solid #1e293b;
      border-radius: 0.5rem;
      padding: 0.75rem 1rem;
      margin-bottom: 1.25rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }
    .dest-selector label {
      font-size: 0.8rem;
      font-weight: 600;
      color: #cbd5e1;
      white-space: nowrap;
    }
    .dest-selector select {
      background: #1e293b;
      color: #f1f5f9;
      border: 1px solid #334155;
      padding: 0.4rem 0.6rem;
      border-radius: 0.375rem;
      font-size: 0.8rem;
      font-weight: 500;
      outline: none;
      width: 100%;
      cursor: pointer;
    }

    .dropzone {
      border: 2px dashed #374151;
      border-radius: 0.75rem;
      padding: 1.75rem 1.25rem;
      text-align: center;
      cursor: pointer;
      background: #0d131f;
      transition: all 0.15s ease;
    }
    .dropzone:hover, .dropzone.dragover {
      border-color: #10b981;
      background: rgba(16, 185, 129, 0.06);
    }
    .dropzone svg { width: 36px; height: 36px; stroke: #34d399; margin-bottom: 0.5rem; }
    .dropzone p { font-size: 0.95rem; font-weight: 600; color: #e2e8f0; }
    .btn-row {
      display: flex;
      gap: 0.75rem;
      margin-top: 1rem;
      justify-content: center;
    }
    .btn {
      background: #1e293b;
      border: 1px solid #334155;
      color: #cbd5e1;
      padding: 0.45rem 0.9rem;
      border-radius: 0.375rem;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn:hover { background: #334155; color: #fff; }
    input[type="file"] { display: none; }

    .lanes-wrap {
      margin-top: 1.25rem;
      display: none;
      gap: 0.6rem;
      flex-direction: column;
    }
    .lane {
      background: #0d131f;
      border: 1px solid #1f2937;
      border-radius: 0.5rem;
      padding: 0.65rem 0.9rem;
    }
    .lane-head {
      display: flex;
      justify-content: space-between;
      font-size: 0.78rem;
      font-weight: 600;
      margin-bottom: 0.3rem;
    }
    .lane-tag {
      font-size: 0.68rem;
      padding: 0.15rem 0.4rem;
      border-radius: 4px;
      text-transform: uppercase;
      font-weight: 700;
    }
    .tag-ch1 { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }
    .tag-ch2 { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .tag-ch3 { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }
    .tag-ch4 { background: rgba(168, 85, 247, 0.15); color: #c084fc; }

    .lane-file {
      font-size: 0.75rem;
      color: #94a3b8;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 65%;
    }
    .track {
      background: #1e293b;
      height: 5px;
      border-radius: 9999px;
      overflow: hidden;
      margin-top: 0.35rem;
    }
    .bar {
      height: 100%;
      width: 0%;
      border-radius: 9999px;
      transition: width 0.08s linear;
    }
    .bar-ch1 { background: #38bdf8; }
    .bar-ch2 { background: #10b981; }
    .bar-ch3 { background: #fbbf24; }
    .bar-ch4 { background: #c084fc; }

    .summary-box {
      margin-top: 0.9rem;
      display: flex;
      justify-content: space-between;
      font-size: 0.8rem;
      color: #94a3b8;
      padding: 0 0.25rem;
    }
    .summary-speed { color: #34d399; font-weight: 700; }
    .status-text {
      margin-top: 0.6rem;
      font-size: 0.8rem;
      text-align: center;
      font-weight: 500;
    }
    .status-text.done { color: #34d399; }
    .status-text.err { color: #f87171; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>FLUX</h1>
      <p class="sub">File-Link-Universal-Exchange</p>
    </div>

    <div class="hw-badge">
      <span class="chip" id="hw-chip">Scanning chip...</span>
      <span id="active-channels-badge">Channels: 2 / max 4</span>
    </div>

    <div class="dest-selector">
      <label for="dest-select">Receiver Folder:</label>
      <select id="dest-select">
        <option value="uploads">FLUX /uploads (Default)</option>
        <option value="downloads">Downloads</option>
        <option value="desktop">Desktop</option>
        <option value="documents">Documents</option>
        <option value="pictures">Pictures</option>
        <option value="videos">Videos</option>
      </select>
    </div>

    <div class="dropzone" id="dz">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <p>Drop files or folders</p>

      <div class="btn-row">
        <button type="button" class="btn" id="bf">Files</button>
        <button type="button" class="btn" id="bd">Folder</button>
      </div>

      <input type="file" id="fi" multiple />
      <input type="file" id="fd" webkitdirectory directory multiple />
    </div>

    <div class="lanes-wrap" id="lanes">
      <!-- Dynamic hardware channels (up to 4) -->
      <div class="lane" id="lane-ch1">
        <div class="lane-head">
          <span class="lane-tag tag-ch1">Channel 1</span>
          <span class="lane-file" id="fn-ch1">Idle</span>
        </div>
        <div class="track"><div class="bar bar-ch1" id="bar-ch1"></div></div>
      </div>

      <div class="lane" id="lane-ch2">
        <div class="lane-head">
          <span class="lane-tag tag-ch2">Channel 2</span>
          <span class="lane-file" id="fn-ch2">Idle</span>
        </div>
        <div class="track"><div class="bar bar-ch2" id="bar-ch2"></div></div>
      </div>

      <div class="lane" id="lane-ch3" style="display:none;">
        <div class="lane-head">
          <span class="lane-tag tag-ch3">Channel 3 (Parallel)</span>
          <span class="lane-file" id="fn-ch3">Idle</span>
        </div>
        <div class="track"><div class="bar bar-ch3" id="bar-ch3"></div></div>
      </div>

      <div class="lane" id="lane-ch4" style="display:none;">
        <div class="lane-head">
          <span class="lane-tag tag-ch4">Channel 4 (Parallel)</span>
          <span class="lane-file" id="fn-ch4">Idle</span>
        </div>
        <div class="track"><div class="bar bar-ch4" id="bar-ch4"></div></div>
      </div>

      <div class="summary-box">
        <span id="transferred">0 files (0 MB)</span>
        <span class="summary-speed" id="speed">0 MB/s</span>
      </div>
      <div class="status-text" id="st">Streaming across parallel hardware channels...</div>
    </div>
  </div>

  <script>
    const dz = document.getElementById('dz');
    const fi = document.getElementById('fi');
    const fd = document.getElementById('fd');
    const bf = document.getElementById('bf');
    const bd = document.getElementById('bd');
    const lanes = document.getElementById('lanes');
    const transferred = document.getElementById('transferred');
    const speed = document.getElementById('speed');
    const st = document.getElementById('st');
    const hwChip = document.getElementById('hw-chip');
    const destSelect = document.getElementById('dest-select');
    const activeBadge = document.getElementById('active-channels-badge');

    const channelElements = [
      { lane: document.getElementById('lane-ch1'), fn: document.getElementById('fn-ch1'), bar: document.getElementById('bar-ch1') },
      { lane: document.getElementById('lane-ch2'), fn: document.getElementById('fn-ch2'), bar: document.getElementById('bar-ch2') },
      { lane: document.getElementById('lane-ch3'), fn: document.getElementById('fn-ch3'), bar: document.getElementById('bar-ch3') },
      { lane: document.getElementById('lane-ch4'), fn: document.getElementById('fn-ch4'), bar: document.getElementById('bar-ch4') }
    ];

    let systemHardware = { cores: navigator.hardwareConcurrency || 4, arch: 'x64', platform: 'desktop' };

    // Fetch initial hardware & destination settings in first few milliseconds
    async function initEnvironment() {
      const t0 = performance.now();
      try {
        const [hwRes, destRes] = await Promise.all([
          fetch('/api/hw').then(r => r.json()),
          fetch('/api/destination').then(r => r.json())
        ]);
        systemHardware = hwRes;
        const scanMs = (performance.now() - t0).toFixed(1);
        hwChip.textContent = hwRes.platform + ' / ' + hwRes.arch + ' • ' + hwRes.cores + ' cores (' + scanMs + 'ms)';

        if (destRes.destinations) {
          destSelect.innerHTML = '';
          for (const d of destRes.destinations) {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.textContent = d.label;
            if (d.id === destRes.activeId) opt.selected = true;
            destSelect.appendChild(opt);
          }
        }
      } catch (err) {
        hwChip.textContent = 'Hardware concurrency: ' + (navigator.hardwareConcurrency || 4) + ' cores';
      }
    }
    initEnvironment();

    destSelect.onchange = async () => {
      const selected = destSelect.value;
      try {
        await fetch('/api/destination', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ destId: selected })
        });
      } catch {}
    };

    bf.onclick = (e) => { e.stopPropagation(); fi.click(); };
    bd.onclick = (e) => { e.stopPropagation(); fd.click(); };
    dz.onclick = () => fi.click();

    dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('dragover'); };
    dz.ondragleave = dz.ondragend = () => dz.classList.remove('dragover');

    function fmt(b) {
      if (!b || b === 0) return '0 B';
      const k = 1024, s = ['B','KB','MB','GB','TB'];
      const i = Math.floor(Math.log(b)/Math.log(k));
      return (b/Math.pow(k, i)).toFixed(2) + ' ' + s[i];
    }

    class AdaptiveExchange {
      constructor() {
        this.queue = [];
        this.totalBytes = 0;
        this.totalFiles = 0;
        this.failedFiles = 0;
        this.discoveryDone = false;
        this.startTime = performance.now();
        this.lastTime = performance.now();
        this.lastBytes = 0;
        this.inFlight = new Map();

        // Hardware-adaptive channel limit (start at 2, expand up to 4 if throughput/idle supports it)
        this.maxAllowedChannels = Math.min(4, Math.max(1, systemHardware.cores || 4));
        this.activeChannelsCount = Math.min(2, this.maxAllowedChannels);

        this.channels = [
          { id: 0, active: false, fnEl: channelElements[0].fn, barEl: channelElements[0].bar },
          { id: 1, active: false, fnEl: channelElements[1].fn, barEl: channelElements[1].bar },
          { id: 2, active: false, fnEl: channelElements[2].fn, barEl: channelElements[2].bar },
          { id: 3, active: false, fnEl: channelElements[3].fn, barEl: channelElements[3].bar }
        ];

        this.updateChannelVisibility();
      }

      updateChannelVisibility() {
        for (let i = 0; i < 4; i++) {
          channelElements[i].lane.style.display = (i < this.activeChannelsCount) ? 'block' : 'none';
        }
        activeBadge.textContent = 'Channels: ' + this.activeChannelsCount + ' / max ' + this.maxAllowedChannels;
      }

      push(file) {
        this.queue.push(file);
        this.drain();
      }

      end() {
        this.discoveryDone = true;
        this.drain();
      }

      drain() {
        // Hardware scale-up check: If queue is populated and speed is high or channels idle, scale up to max 4
        if (this.queue.length > 2 && this.activeChannelsCount < this.maxAllowedChannels) {
          this.activeChannelsCount = Math.min(this.maxAllowedChannels, this.activeChannelsCount + 1);
          this.updateChannelVisibility();
        }

        // Assign queued files to any idle parallel channel
        for (let i = 0; i < this.activeChannelsCount; i++) {
          const ch = this.channels[i];
          if (!ch.active && this.queue.length > 0) {
            const file = this.queue.shift();
            ch.active = true;
            this.stream(file, ch);
          }
        }

        const allIdle = this.channels.every(c => !c.active);
        if (this.discoveryDone && this.queue.length === 0 && allIdle) {
          this.finish();
        }
      }

      updateTelemetry() {
        let inFlightTotal = 0;
        this.inFlight.forEach(v => inFlightTotal += v);
        const overall = this.totalBytes + inFlightTotal;
        transferred.textContent = this.totalFiles + ' files' + (this.failedFiles > 0 ? (' (' + this.failedFiles + ' failed)') : '') + ' (' + fmt(overall) + ')';

        const now = performance.now();
        const dt = (now - this.lastTime) / 1000;
        if (dt >= 0.15) {
          const instantSpeed = (overall - this.lastBytes) / dt;
          speed.textContent = fmt(instantSpeed) + '/s';

          // If throughput supports > 20 MB/s and pending files remain, dynamically expand channels up to max 4
          if (instantSpeed > 20 * 1024 * 1024 && this.activeChannelsCount < this.maxAllowedChannels) {
            this.activeChannelsCount = Math.min(this.maxAllowedChannels, this.activeChannelsCount + 1);
            this.updateChannelVisibility();
          }

          this.lastBytes = overall;
          this.lastTime = now;
        }
      }

      async stream(file, channel) {
        const path = file.relativePath || file.name;
        channel.fnEl.textContent = path;
        channel.barEl.style.width = '0%';

        await new Promise((resolve) => {
          const xhr = new XMLHttpRequest();
          const id = Symbol();

          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              this.inFlight.set(id, e.loaded);
              const p = Math.round((e.loaded / e.total) * 100);
              channel.barEl.style.width = p + '%';
              this.updateTelemetry();
            }
          };

          xhr.onload = () => {
            this.inFlight.delete(id);
            if (xhr.status >= 200 && xhr.status < 300) {
              channel.barEl.style.width = '100%';
              this.totalBytes += file.size;
              this.totalFiles++;
            } else {
              this.failedFiles++;
              channel.fnEl.textContent = path + ' (Failed ' + xhr.status + ')';
            }
            this.updateTelemetry();
            resolve();
          };

          xhr.onerror = () => {
            this.inFlight.delete(id);
            this.failedFiles++;
            channel.fnEl.textContent = path + ' (Network error)';
            this.updateTelemetry();
            resolve();
          };

          xhr.open('PUT', '/upload');
          xhr.setRequestHeader('X-Relative-Path', encodeURIComponent(path));
          xhr.setRequestHeader('X-File-Size', file.size.toString());
          xhr.setRequestHeader('Content-Type', 'application/octet-stream');
          xhr.send(file);
        });

        channel.active = false;
        setTimeout(() => {
          if (!channel.active) {
            channel.fnEl.textContent = 'Idle';
            channel.barEl.style.width = '0%';
          }
        }, 500);
        this.drain();
      }

      finish() {
        const dur = (performance.now() - this.startTime) / 1000;
        const avg = this.totalBytes / (dur || 1);
        speed.textContent = 'Avg: ' + fmt(avg) + '/s';
        if (this.failedFiles === 0) {
          st.textContent = this.totalFiles + ' files completed in ' + dur.toFixed(2) + 's (' + fmt(avg) + '/s) ✓';
          st.className = 'status-text done';
        } else if (this.totalFiles === 0) {
          st.textContent = 'Transfer failed: ' + this.failedFiles + ' files could not be saved ✗';
          st.className = 'status-text err';
        } else {
          st.textContent = this.totalFiles + ' completed, ' + this.failedFiles + ' failed in ' + dur.toFixed(2) + 's (' + fmt(avg) + '/s)';
          st.className = 'status-text err';
        }
        fi.value = '';
        fd.value = '';
      }
    }

    function scan(entry, p, exchange) {
      if (entry.isFile) {
        entry.file(f => {
          f.relativePath = p ? (p + '/' + f.name) : f.name;
          exchange.push(f);
        });
      } else if (entry.isDirectory) {
        const next = p ? (p + '/' + entry.name) : entry.name;
        const r = entry.createReader();
        function read() {
          r.readEntries(b => {
            if (b && b.length > 0) {
              for (let i = 0; i < b.length; i++) scan(b[i], next, exchange);
              read();
            }
          });
        }
        read();
      }
    }

    dz.ondrop = (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      lanes.style.display = 'flex';
      st.className = 'status-text';
      st.textContent = 'Streaming across parallel hardware channels...';

      const exchange = new AdaptiveExchange();
      const items = e.dataTransfer.items;

      if (items && items.length > 0 && items[0].webkitGetAsEntry) {
        for (let i = 0; i < items.length; i++) {
          const entry = items[i].webkitGetAsEntry();
          if (entry) scan(entry, '', exchange);
        }
      } else if (e.dataTransfer.files) {
        for (let i = 0; i < e.dataTransfer.files.length; i++) {
          const f = e.dataTransfer.files[i];
          f.relativePath = f.webkitRelativePath || f.name;
          exchange.push(f);
        }
      }
      setTimeout(() => exchange.end(), 600);
    };

    fi.onchange = (e) => {
      lanes.style.display = 'flex';
      const exchange = new AdaptiveExchange();
      for (const f of e.target.files) { f.relativePath = f.name; exchange.push(f); }
      exchange.end();
    };

    fd.onchange = (e) => {
      lanes.style.display = 'flex';
      const exchange = new AdaptiveExchange();
      for (const f of e.target.files) { f.relativePath = f.webkitRelativePath || f.name; exchange.push(f); }
      exchange.end();
    };
  </script>
</body>
</html>`;

serve({
  port: PORT,
  hostname: "0.0.0.0",
  reusePort: true,
  maxRequestBodySize: 1024 * 1024 * 1024 * 100,
  async fetch(req) {
    const url = new URL(req.url);

    if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return new Response(HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }

    // Hardware Telemetry API (detected in nanoseconds/milliseconds)
    if (req.method === "GET" && url.pathname === "/api/hw") {
      const currentNs = Bun.nanoseconds ? Bun.nanoseconds() : Number(process.hrtime.bigint());
      return new Response(JSON.stringify({
        ...detectedHardware,
        uptimeNs: currentNs - bootNs,
        serverTime: Date.now()
      }), {
        headers: { "Content-Type": "application/json" }
      });
    }

    // Receiver Destination folder API
    if (req.method === "GET" && url.pathname === "/api/destination") {
      return new Response(JSON.stringify({
        activeId: activeConfig.destId,
        activePath: activeConfig.destPath,
        destinations: SAFE_DESTINATIONS.map(d => ({ id: d.id, label: d.label }))
      }), {
        headers: { "Content-Type": "application/json" }
      });
    }

    if (req.method === "POST" && url.pathname === "/api/destination") {
      try {
        const body = await req.json();
        const verified = sanitizeDestination(body.destId);
        activeConfig = { destId: verified.id, destPath: verified.path };
        await mkdir(activeConfig.destPath, { recursive: true });
        await writeFile(CONFIG_PATH, JSON.stringify(activeConfig, null, 2), "utf8");
        return new Response(JSON.stringify({ ok: true, activeId: activeConfig.destId }), {
          headers: { "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: err.message }), {
          status: 400,
          headers: { "Content-Type": "application/json" }
        });
      }
    }

    // Stream upload with transfer indicator window
    if (req.method === "PUT" && url.pathname === "/upload") {
      try {
        const raw = decodeURIComponent(req.headers.get("X-Relative-Path") || "file");
        // Strict path traversal prevention: strip any leading slashes and dot-dot path segments
        const safeSegments = raw.replace(/^[/\\]+/, "").split(/[/\\]+/).filter(seg => seg && seg !== "." && seg !== "..");
        const clean = safeSegments.join("/") || "file";
        const target = resolve(activeConfig.destPath, clean);

        // Enforce privilege boundary: target must reside strictly under the active safe destination
        if (!isUnderDirectory(target, activeConfig.destPath)) {
          return new Response("Forbidden destination path", { status: 403 });
        }

        const sizeHeader = req.headers.get("X-File-Size") || "";
        showTransferWindow(clean, sizeHeader);

        await mkdir(dirname(target), { recursive: true });
        if (!req.body) {
          dismissTransferWindow();
          return new Response("Empty", { status: 400 });
        }

        await Bun.write(target, req.body);
        dismissTransferWindow();
        return new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json" } });
      } catch (err) {
        dismissTransferWindow();
        return new Response(err.message, { status: 500 });
      }
    }

    return new Response("404 Not Found", { status: 404 });
  }
});

console.log("FLUX active on port " + PORT + " | Safe Receiver: " + activeConfig.destId);