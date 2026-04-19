# Sagacious 💡

A floating macOS app that watches your Terminal and explains what's happening in plain English — in real time.

Powered by your choice of a **local Ollama model** (free, private) or **Claude Haiku** (fast, cloud).

![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon-black?logo=apple)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What it does

Sagacious sits in the corner of your screen, reads your Terminal with OCR, and tells you:

- **Events** — what specific command just ran or failed
- **Process** — what task is in progress and what phase
- **Overview** — the big picture in one sentence

You can also ask it questions ("why did that fail?") via the built-in chat.

---

## Install (recommended — pre-built app)

1. Download `Sagacious.app` from the [latest release](../../releases/latest)
2. Move it to your `/Applications` folder
3. Open it — macOS may warn about an unverified developer; go to **System Settings → Privacy & Security → Open Anyway**
4. Grant **Screen Recording** permission when prompted (required to see your Terminal)

---

## Install from source

**Requirements:** macOS, Python 3.10+, [Homebrew](https://brew.sh)

```bash
# 1. Clone the repo
git clone https://github.com/sidvarma/sagacious.git
cd sagacious

# 2. Install Tesseract (OCR engine)
brew install tesseract

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Run
python app.py
```

> **Grant Screen Recording permission** to Terminal (or your IDE) in System Settings → Privacy & Security → Screen Recording. Sagacious needs to see your Terminal window.

---

## Choose your AI provider

When you first open Sagacious, tap the ⚙ gear icon to pick:

### Option A — Local model (free, private)
1. Download [Ollama](https://ollama.com)
2. Pull a model: `ollama pull llama3.2`
3. In Sagacious settings, select **Local Model** and choose your model

### Option B — Claude Haiku (faster, smarter)
1. Get a free API key at [console.anthropic.com](https://console.anthropic.com)
2. In Sagacious settings, select **Claude Haiku** and paste your key

---

## Build the .app yourself

```bash
pip install pyinstaller
pyinstaller Sagacious.spec
# Output: dist/Sagacious.app
```

---

## Requirements

| Dependency | Purpose |
|---|---|
| PyQt6 | UI framework |
| Pillow | Screen capture |
| pytesseract | OCR (reads Terminal text) |
| anthropic | Claude Haiku API |
| requests | Ollama API |
| pyobjc (Quartz) | Terminal window bounds detection |

---

## Permissions needed

| Permission | Why |
|---|---|
| Screen Recording | Read your Terminal window via OCR |
| Accessibility | Detect which window is in focus (optional) |

---

## License

MIT
