# USB Speed Test

A cross-platform USB read/write speed tester with a live web UI — no installation required beyond Python 3.

<img width="781" height="370" alt="Screenshot 2026-04-24 at 12 48 05 PM" src="https://github.com/user-attachments/assets/09d1c8c5-0b58-4473-b83b-9e4b3a716bd0" />


---

## Features

- **Real-time gauges and graphs** — live MB/s chart updates as the test runs
- **Accurate results** — bypasses the OS page cache (`F_NOCACHE` on macOS) so reads reflect actual drive throughput, not RAM
- **Drag and drop** — drag your USB volume icon (or any file from it) onto the browser window to auto-select the drive
- **Cross-platform** — works on macOS, Windows, and Linux
- **Zero dependencies** — pure Python standard library, no `pip install` needed
- **Auto-opens browser** — just run the script and the UI appears

---

## Requirements

| Requirement | Details |
|---|---|
| **Python 3.7+** | [python.org/downloads](https://www.python.org/downloads/) |
| **Internet connection** | Chart.js is loaded from a CDN (jsDelivr) the first time |
| **A USB drive** | Must be writable and mounted before starting |

That's it. No `pip install`, no virtual environment, no Node.js.

### OS-specific notes

| OS | Drive detection method | Notes |
|---|---|---|
| macOS | `diskutil` (built-in) | Detects external physical drives automatically |
| Windows | PowerShell `Get-WmiObject` / `wmic` fallback | Shows removable drives (DriveType 2) |
| Linux | `lsblk` (usually pre-installed) | Shows hotplug-mounted partitions |

---

## How to run

```bash
# Clone
git clone https://github.com/Code-Project-one/USB-Speed-Test.git
cd usb-speed-test

# Run
python3 usb_speedtest.py          # macOS / Linux
python  usb_speedtest.py          # Windows
```

The browser opens automatically at `http://localhost:7789`.  
Press `Ctrl+C` in the terminal to stop the server.

---

## How to use

1. Plug in your USB drive
2. The drive appears in the dropdown — or drag the volume icon / any file from it onto the page to auto-select
3. Choose a test size (64 MB → 1 GB)
4. Click **Start Test**
5. Watch live write then read speeds; results appear at the bottom when done

---

## How it works

```
python3 usb_speedtest.py
       │
       ├── starts HTTP server on localhost:7789
       ├── opens your browser
       │
       ├── /api/drives      → detects mounted USB drives (OS-specific)
       ├── /api/identify    → matches a dropped file/folder to a drive path
       ├── /api/start       → begins test in a background thread
       ├── /api/stream      → Server-Sent Events push live speed data to browser
       └── /api/stop        → cancels a running test
```

**Write test** — writes 1 MB chunks of random data with `fsync` after each chunk to force the OS to flush to the physical drive.

**Read test** — reads the same file back with the OS page cache disabled (`F_NOCACHE` on macOS, standard on Windows/Linux) so the kernel cannot serve results from RAM.

**Drag and drop** — uses `webkitGetAsEntry()` to identify whether a directory or file was dropped, then calls `/api/identify` which matches by volume name, file existence, or fuzzy path substring.

---

## Speed ratings

| Rating | Threshold |
|---|---|
| FAST | ≥ 100 MB/s |
| GOOD | ≥ 30 MB/s |
| SLOW | < 30 MB/s |

---

## Project structure

```
usb-speed-test/
├── usb_speedtest.py   # everything — backend + embedded HTML/CSS/JS
└── README.md
```

The entire UI (HTML, CSS, JavaScript) is embedded as a string inside `usb_speedtest.py` so there is only one file to share or run.

---

## Contributing

Pull requests are welcome. Some ideas:

- Bundle Chart.js locally so the tool works fully offline
- Add IOPS / latency measurement
- Add a history log of past test results
- Dark/light theme toggle
- Export results as JSON or CSV

---

## License

MIT License — do whatever you want with it, no warranty implied.

```
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
