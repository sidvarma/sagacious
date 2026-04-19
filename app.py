#!/usr/bin/env python3
import sys, os, json, hashlib, io, base64, subprocess, time, datetime, requests
from pathlib import Path

# Ensure Homebrew binaries are found when launched from Finder (no shell PATH)
os.environ['PATH'] = '/opt/homebrew/bin:/usr/local/bin:' + os.environ.get('PATH', '')

import pytesseract
pytesseract.pytesseract.tesseract_cmd = '/opt/homebrew/bin/tesseract'
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QScrollArea, QFrame, QPushButton, QLineEdit,
    QRadioButton, QButtonGroup, QStackedWidget, QSizePolicy, QMenu)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QCursor
from PIL import ImageGrab
import anthropic

CONFIG_PATH = Path.home() / ".terminal-explainer.json"
OLLAMA_URL  = "http://localhost:11434/api/chat"
INTERVAL    = 5
MAX_BUBBLES = 12

# ── Config helpers ────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"provider": "ollama", "api_key": ""}

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

# ── Terminal capture & OCR ────────────────────────────────────────────────────

def get_terminal_bounds():
    try:
        from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        for w in windows:
            if w.get('kCGWindowOwnerName') == 'Terminal' and w.get('kCGWindowLayer', 99) == 0:
                b = w.get('kCGWindowBounds')
                if b and b['Width'] > 0 and b['Height'] > 0:
                    x, y = int(b['X']), int(b['Y'])
                    ww, wh = int(b['Width']), int(b['Height'])
                    return (x, y, x + ww, y + wh)
    except Exception:
        pass
    return None

def capture_terminal():
    b = get_terminal_bounds()
    if not b:
        return None
    try:
        return ImageGrab.grab(bbox=b)
    except:
        return None

def extract_text(img):
    w,h = img.size
    if w > 1400: img = img.resize((w//2, h//2))
    raw = pytesseract.image_to_string(img)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    return "\n".join(lines[-50:])

def text_hash(t):
    return hashlib.md5(t.encode()).hexdigest()

def first_sentence(text, word_cap=18):
    import re
    text = text.strip().strip('"').strip("'")
    parts = re.split(r'(?<=[.!?])\s', text)
    sentence = parts[0].strip() if parts else text
    words = sentence.split()
    if len(words) > word_cap:
        sentence = " ".join(words[:word_cap]).rstrip(",;:") + "."
    return sentence

# ── AI providers ──────────────────────────────────────────────────────────────

PROMPTS = {
    "events": {
        "system": """You are a granular terminal event logger. Report specific individual actions as they happen.
Be precise: name the exact command, file, error, or action — not a vague summary.
Examples: "pip failed — package 'requests' not found.", "Git push rejected — remote has new commits.", "Port 3000 is already in use."
Only skip if absolutely nothing happened (idle prompt). Max 12 words.""",
        "user": "Terminal:\n{text}\n\nWhat specific event just occurred? Name it exactly. 12 words max or SKIP.",
    },
    "process": {
        "system": """You describe what task is currently in progress and what phase it's at.
Focus on the active job: what is running, how far along, what it's doing now.
Examples: "npm is installing dependencies, about halfway through.", "Python script is running and waiting for input.", "Build succeeded — starting dev server now."
If nothing is actively running, reply SKIP. Max 15 words.""",
        "user": "Terminal:\n{text}\n\nWhat task is in progress and what phase? 15 words max or SKIP.",
    },
    "overview": {
        "system": """You give a single broad summary of what's generally happening — the big picture, not details.
Think: if someone glanced at the terminal for one second, what would they say is going on?
Examples: "Setting up a new project.", "Running tests.", "Server is live.", "Something crashed."
Only update when the overall situation changes. Max 10 words.""",
        "user": "Terminal:\n{text}\n\nBig picture: what's generally going on? 10 words max or SKIP.",
    },
}

# Intervals per mode (seconds between checks)
MODE_INTERVALS = {"events": 5, "process": 4, "overview": 10}

MODE_WORD_CAPS = {"events": 12, "process": 15, "overview": 10}

def explain_ollama(text, model="llama3.2", mode="events"):
    if not text.strip():
        return "SKIP"
    p = PROMPTS[mode]
    r = requests.post(OLLAMA_URL, json={
        "model": model,
        "messages": [
            {"role": "system", "content": p["system"]},
            {"role": "user",   "content": p["user"].format(text=text)}
        ],
        "stream": False,
        "options": {"num_predict": 50, "temperature": 0.2}
    }, timeout=30)
    r.raise_for_status()
    return first_sentence(r.json()["message"]["content"], MODE_WORD_CAPS[mode])

def explain_claude(text, api_key, mode="events"):
    if not text.strip():
        return "SKIP"
    p = PROMPTS[mode]
    client = anthropic.Anthropic(api_key=api_key)

    img = capture_terminal()
    if img:
        buf = io.BytesIO()
        w,h = img.size
        if w > 1400: img = img.resize((w//2, h//2))
        img.save(buf, format='PNG')
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        content = [
            {"type":"image","source":{"type":"base64","media_type":"image/png","data":b64}},
            {"type":"text","text": p["user"].format(text=text)}
        ]
    else:
        content = [{"type":"text","text": p["user"].format(text=text)}]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        system=p["system"],
        messages=[{"role":"user","content":content}]
    )
    return first_sentence(resp.content[0].text, MODE_WORD_CAPS.get(mode, 15))

def check_ollama():
    try: requests.get("http://localhost:11434", timeout=2); return True
    except: return False

# ── Worker thread ─────────────────────────────────────────────────────────────

class CaptureWorker(QThread):
    new_explanation = pyqtSignal(str, str)  # (text, mode)
    status_changed  = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config       = config
        self.running      = True
        self.paused       = False
        self.last_hash    = None
        self.last_message = None
        self.last_text    = ""
        self.mode         = config.get("mode", "events")

    def update_config(self, config):
        self.config = config
        self.mode   = config.get("mode", "events")
        self.last_hash    = None
        self.last_message = None

    def set_mode(self, mode):
        self.mode         = mode
        self.last_hash    = None
        self.last_message = None

    def set_paused(self, paused):
        self.paused = paused
        self.last_hash    = None
        self.last_message = None

    def run(self):
        while self.running:
            if self.paused:
                self.status_changed.emit("paused")
                time.sleep(1); continue

            provider = self.config.get("provider","ollama")

            if provider == "ollama" and not check_ollama():
                self.status_changed.emit("no_ollama"); time.sleep(3); continue

            if provider == "claude" and not self.config.get("api_key",""):
                self.status_changed.emit("no_key"); time.sleep(3); continue

            img = capture_terminal()
            if img is None:
                self.status_changed.emit("no_terminal"); time.sleep(INTERVAL); continue

            try:
                text = extract_text(img)
                self.last_text = text
                h = text_hash(text)
                if h != self.last_hash:
                    self.last_hash = h
                    self.status_changed.emit("thinking")
                    if provider == "claude":
                        result = explain_claude(text, self.config["api_key"], self.mode)
                    else:
                        result = explain_ollama(text, self.config.get("ollama_model","llama3.2"), self.mode)
                    if result.strip().upper() != "SKIP" and result != self.last_message:
                        self.last_message = result
                        self.new_explanation.emit(result, self.mode)
                self.status_changed.emit("watching")
            except Exception:
                self.status_changed.emit("watching")

            time.sleep(MODE_INTERVALS.get(self.mode, INTERVAL))

    def stop(self): self.running = False

# ── UI components ─────────────────────────────────────────────────────────────

CHAT_SYSTEM = """You are a helpful assistant answering questions about what's happening in a terminal. The user can see their terminal and may ask about it. Reply in ONE short plain-English sentence. No jargon, no technical words. Be specific and direct."""

def chat_reply(question, terminal_text, config):
    provider = config.get("provider", "ollama")
    context = f"Terminal context:\n{terminal_text}\n\nUser question: {question}" if terminal_text.strip() else f"User question: {question}"
    if provider == "claude" and config.get("api_key"):
        client = anthropic.Anthropic(api_key=config["api_key"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=CHAT_SYSTEM,
            messages=[{"role": "user", "content": context}]
        )
        return first_sentence(resp.content[0].text)
    else:
        model = config.get("ollama_model", "llama3.2")
        r = requests.post(OLLAMA_URL, json={
            "model": model,
            "messages": [
                {"role": "system", "content": CHAT_SYSTEM},
                {"role": "user",   "content": context}
            ],
            "stream": False,
            "options": {"num_predict": 60, "temperature": 0.3}
        }, timeout=30)
        r.raise_for_status()
        return first_sentence(r.json()["message"]["content"])


class ChatWorker(QThread):
    reply_ready = pyqtSignal(str, str)  # (question, answer)

    def __init__(self, question, terminal_text, config):
        super().__init__()
        self.question      = question
        self.terminal_text = terminal_text
        self.config        = config

    def run(self):
        try:
            answer = chat_reply(self.question, self.terminal_text, self.config)
        except Exception as e:
            answer = "Couldn't get an answer right now — try again."
        self.reply_ready.emit(self.question, answer)


# ── Design tokens (Apple glass dark) ─────────────────────────────────────────
BG      = "rgba(13, 13, 23, 248)"
GLASS   = "rgba(255, 255, 255, 7)"
GLASS2  = "rgba(255, 255, 255, 12)"
BORDER  = "rgba(255, 255, 255, 18)"
BORD2   = "rgba(255, 255, 255, 8)"
TEXT    = "#f2f2f7"
SOFT    = "#aaaabe"
MUTED   = "#5e5e76"
GREEN   = "#32d74b"
YELLOW  = "#ffd60a"
RED     = "#ff453a"
ACCENT  = "#6366f1"
PURPLE  = "#a855f7"

BTN_STYLE = f"""
QPushButton {{
    background: {ACCENT}; color: #ffffff; border-radius: 8px;
    padding: 7px 16px; font-size: 12px; font-weight: 600; border: none;
}}
QPushButton:hover  {{ background: #7577f5; }}
QPushButton:pressed {{ background: #4f51d4; }}
QPushButton:disabled {{ background: rgba(99,102,241,80); color: rgba(255,255,255,100); }}
"""

RADIO_STYLE = f"""
QRadioButton {{ color: {SOFT}; font-size: 13px; background: transparent; spacing: 8px; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 8px;
    border: 1.5px solid {MUTED}; background: transparent; }}
QRadioButton::indicator:checked {{ border: 1.5px solid {ACCENT}; background: {ACCENT}; }}
"""

INPUT_STYLE = f"""
QLineEdit {{
    background: rgba(255,255,255,8); color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 20px;
    padding: 9px 16px; font-size: 13px;
}}
QLineEdit:focus {{ border: 1px solid rgba(99,102,241,180); background: rgba(255,255,255,11); }}
"""

SETTINGS_INPUT = f"""
QLineEdit {{
    background: rgba(255,255,255,8); color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 10px;
    padding: 8px 12px; font-size: 13px;
}}
QLineEdit:focus {{ border: 1px solid rgba(99,102,241,180); }}
"""

MENU_STYLE = f"""
QMenu {{
    background: #16162e; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 12px;
    padding: 6px; font-size: 13px;
}}
QMenu::item {{ padding: 9px 14px; border-radius: 8px; }}
QMenu::item:selected {{ background: rgba(99,102,241,90); color: {TEXT}; }}
QMenu::separator {{ height: 1px; background: {BORD2}; margin: 4px 10px; }}
"""

OLLAMA_MODELS = [
    ("llama3.2",    "llama3.2",    "Meta · 3B · fast & lightweight"),
    ("llama3.1",    "llama3.1",    "Meta · 8B · great all-rounder"),
    ("llama3.3",    "llama3.3",    "Meta · 70B · most powerful"),
    ("mistral",     "mistral",     "Mistral · 7B · sharp & efficient"),
    ("gemma3",      "gemma3",      "Google · 4B · small but capable"),
    ("phi4",        "phi4",        "Microsoft · 14B · reasoning"),
    ("qwen2.5",     "qwen2.5",     "Alibaba · 7B · multilingual"),
    ("deepseek-r1", "deepseek-r1", "DeepSeek · 7B · strong reasoning"),
    ("codellama",   "codellama",   "Meta · 7B · code focused"),
    ("tinyllama",   "tinyllama",   "1.1B · ultra-fast · low RAM"),
]


class ModelDropdown(QPushButton):
    model_changed = pyqtSignal(str)

    def __init__(self, saved_model):
        super().__init__()
        self._value = saved_model
        self.setFixedHeight(44)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._base_style = f"""
            QPushButton {{
                background: rgba(255,255,255,8); color: {TEXT};
                border: 1px solid {BORDER}; border-radius: 11px;
                padding: 0 14px; font-size: 13px; text-align: left;
            }}
            QPushButton:hover {{ background: rgba(255,255,255,13); border: 1px solid rgba(255,255,255,28); }}
            QPushButton:pressed {{ background: rgba(255,255,255,6); }}
        """
        self.setStyleSheet(self._base_style)
        self._refresh_label()
        self.clicked.connect(self._open_menu)

    def _refresh_label(self):
        desc = next((d for v,_,d in OLLAMA_MODELS if v == self._value), "")
        display = f"  {self._value}"
        if desc:
            display += f"    {desc}"
        # chevron appended via layout trick — we paint it in the text
        self.setText(display + "      ⌄")

    def _open_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        for value, name, desc in OLLAMA_MODELS:
            action = menu.addAction(f"{name}   ·   {desc}")
            action.setData(value)
            if value == self._value:
                action.setCheckable(True)
                action.setChecked(True)
        chosen = menu.exec(self.mapToGlobal(self.rect().bottomLeft()))
        if chosen and chosen.data():
            self._value = chosen.data()
            self._refresh_label()
            self.model_changed.emit(self._value)

    def current_value(self):
        return self._value


class SegmentedControl(QFrame):
    changed = pyqtSignal(str)

    SEGMENTS = [
        ("events",   "Events"),
        ("process",  "Process"),
        ("overview", "Overview"),
    ]

    def __init__(self, current="events"):
        super().__init__()
        self.setFixedHeight(32)
        self.setStyleSheet(f"QFrame{{background:rgba(255,255,255,6);border:1px solid {BORD2};border-radius:10px;}}")
        lay = QHBoxLayout(self); lay.setContentsMargins(3,3,3,3); lay.setSpacing(2)
        self._btns = {}
        for value, label in self.SEGMENTS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda _, v=value: self._select(v))
            self._btns[value] = btn
            lay.addWidget(btn)
        self._select(current, emit=False)

    def _select(self, value, emit=True):
        self._current = value
        for v, btn in self._btns.items():
            if v == value:
                btn.setStyleSheet(f"""QPushButton{{
                    background:rgba(99,102,241,200);color:#ffffff;
                    border:none;border-radius:7px;
                    font-size:11px;font-weight:600;
                }}""")
            else:
                btn.setStyleSheet(f"""QPushButton{{
                    background:transparent;color:{MUTED};
                    border:none;border-radius:7px;font-size:11px;
                }}
                QPushButton:hover{{color:{SOFT};background:rgba(255,255,255,6);}}""")
        if emit:
            self.changed.emit(value)

    def current(self):
        return self._current


class Bubble(QFrame):
    def __init__(self, text, provider, question=None):
        super().__init__()
        is_chat = question is not None
        accent  = PURPLE if is_chat else (ACCENT if provider == "claude" else GREEN)
        bg      = "rgba(120,60,200,10)" if is_chat else GLASS
        border  = "rgba(168,85,247,35)" if is_chat else BORDER
        self.setStyleSheet(f"QFrame{{background:{bg};border-radius:14px;border:1px solid {border};}}")
        lay = QVBoxLayout(self); lay.setContentsMargins(14,11,14,10); lay.setSpacing(5)
        if is_chat:
            q_lbl = QLabel(question); q_lbl.setWordWrap(True)
            q_lbl.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;border:none;font-style:italic;")
            lay.addWidget(q_lbl)
        msg = QLabel(text); msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{TEXT};font-size:13px;background:transparent;border:none;")
        msg.setFont(QFont("SF Pro Text", 13)); lay.addWidget(msg)
        foot = QHBoxLayout(); foot.setContentsMargins(0,2,0,0)
        ts = QLabel(datetime.datetime.now().strftime("%H:%M"))
        ts.setStyleSheet(f"color:{MUTED};font-size:10px;background:transparent;border:none;")
        badge_text = "you asked" if is_chat else ("Claude Haiku" if provider == "claude" else provider)
        badge = QLabel(badge_text)
        badge.setStyleSheet(f"color:{accent};font-size:10px;background:transparent;border:none;font-weight:600;letter-spacing:0.3px;")
        foot.addWidget(ts); foot.addStretch(); foot.addWidget(badge)
        lay.addLayout(foot)


class ProviderCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, icon, title, subtitle):
        super().__init__()
        self.setFixedHeight(64)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._selected = False
        self._update_style()
        lay = QHBoxLayout(self); lay.setContentsMargins(14,0,14,0); lay.setSpacing(12)
        ico = QLabel(icon); ico.setStyleSheet("background:transparent;border:none;font-size:20px;")
        ico.setFixedWidth(28)
        txt = QWidget(); txt.setStyleSheet("background:transparent;")
        tl = QVBoxLayout(txt); tl.setContentsMargins(0,0,0,0); tl.setSpacing(1)
        t = QLabel(title); t.setStyleSheet(f"color:{TEXT};font-size:13px;font-weight:600;background:transparent;border:none;")
        s = QLabel(subtitle); s.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;border:none;")
        tl.addWidget(t); tl.addWidget(s)
        lay.addWidget(ico); lay.addWidget(txt); lay.addStretch()
        self.check = QLabel("✓")
        self.check.setStyleSheet(f"color:{ACCENT};font-size:14px;font-weight:700;background:transparent;border:none;")
        self.check.setVisible(False)
        lay.addWidget(self.check)

    def _update_style(self):
        if self._selected:
            self.setStyleSheet(f"QFrame{{background:rgba(99,102,241,18);border:1.5px solid rgba(99,102,241,160);border-radius:14px;}}")
        else:
            self.setStyleSheet(f"QFrame{{background:{GLASS};border:1px solid {BORDER};border-radius:14px;}}")

    def set_selected(self, val):
        self._selected = val
        self.check.setVisible(val)
        self._update_style()

    def mousePressEvent(self, e):
        self.clicked.emit()


class SettingsPanel(QFrame):
    saved = pyqtSignal(dict)

    def __init__(self, config):
        super().__init__()
        self.setStyleSheet("QFrame{background:transparent;border:none;}")

        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)

        # Scrollable content area
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea{{border:none;background:transparent;}}
            QScrollBar:vertical{{width:3px;background:transparent;}}
            QScrollBar::handle:vertical{{background:rgba(255,255,255,20);border-radius:1px;}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0px;}}
        """)

        content = QWidget(); content.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(content); lay.setContentsMargins(20,12,20,16); lay.setSpacing(20)

        # ── Provider section ───────────────────────────────────────────────────
        self._section_label(lay, "AI PROVIDER")

        self.card_local  = ProviderCard("🦙", "Local Model", "Free · private · runs on your Mac")
        self.card_claude = ProviderCard("✦", "Claude Haiku", "Faster · smarter · needs API key")
        self.card_local.clicked.connect(lambda: self._select_provider(False))
        self.card_claude.clicked.connect(lambda: self._select_provider(True))

        lay.addWidget(self.card_local)
        lay.addWidget(self.card_claude)

        # ── Local model section ────────────────────────────────────────────────
        self.ollama_section = QWidget(); self.ollama_section.setStyleSheet("background:transparent;")
        ol = QVBoxLayout(self.ollama_section); ol.setContentsMargins(0,0,0,0); ol.setSpacing(10)

        self._section_label(ol, "MODEL")

        self.model_btn = ModelDropdown(config.get("ollama_model", "llama3.2"))
        ol.addWidget(self.model_btn)

        # Guide card
        guide = QFrame()
        guide.setStyleSheet(f"QFrame{{background:rgba(99,102,241,10);border:1px solid rgba(99,102,241,30);border-radius:12px;}}")
        gl = QVBoxLayout(guide); gl.setContentsMargins(14,12,14,12); gl.setSpacing(6)
        gh = QLabel("Don't have Ollama yet?")
        gh.setStyleSheet(f"color:{SOFT};font-size:12px;font-weight:600;background:transparent;border:none;")
        gl.addWidget(gh)
        for step in ["1  Download Ollama → ollama.com", "2  Open Terminal and run:", "     ollama pull <model-name>", "3  Browse free models → ollama.com/library"]:
            l = QLabel(step); l.setWordWrap(True)
            l.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;border:none;")
            gl.addWidget(l)
        ol.addWidget(guide)
        lay.addWidget(self.ollama_section)

        # ── Claude section ─────────────────────────────────────────────────────
        self.claude_section = QWidget(); self.claude_section.setStyleSheet("background:transparent;")
        cl = QVBoxLayout(self.claude_section); cl.setContentsMargins(0,0,0,0); cl.setSpacing(10)

        self._section_label(cl, "API KEY")

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("sk-ant-...")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setStyleSheet(SETTINGS_INPUT)
        self.key_input.setFixedHeight(40)
        self.key_input.setText(config.get("api_key",""))
        cl.addWidget(self.key_input)

        hint = QLabel("Get your free key at console.anthropic.com")
        hint.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;border:none;")
        cl.addWidget(hint)
        lay.addWidget(self.claude_section)

        lay.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # Save button — fixed at bottom, outside scroll
        btn_wrap = QWidget(); btn_wrap.setStyleSheet("background:transparent;")
        bl = QVBoxLayout(btn_wrap); bl.setContentsMargins(20,8,20,16)
        save_btn = QPushButton("Save & Apply")
        save_btn.setStyleSheet(BTN_STYLE)
        save_btn.setFixedHeight(42)
        save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        save_btn.clicked.connect(self._save)
        bl.addWidget(save_btn)
        outer.addWidget(btn_wrap)

        is_claude = config.get("provider","ollama") == "claude"
        self._select_provider(is_claude, init=True)

    def _section_label(self, layout, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{MUTED};font-size:10px;font-weight:700;letter-spacing:1.2px;background:transparent;border:none;")
        layout.addWidget(lbl)

    def _select_provider(self, claude_on, init=False):
        self._claude_on = claude_on
        self.card_local.set_selected(not claude_on)
        self.card_claude.set_selected(claude_on)
        self.claude_section.setVisible(claude_on)
        self.ollama_section.setVisible(not claude_on)

    def _save(self):
        cfg = {
            "provider":     "claude" if self._claude_on else "ollama",
            "api_key":      self.key_input.text().strip(),
            "ollama_model": self.model_btn.current_value() or "llama3.2",
        }
        save_config(cfg)
        self.saved.emit(cfg)


class ExplainerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config    = load_config()
        self.bubbles   = []
        self._drag_pos = None
        self._dot_on   = True
        self._ui()
        self._start_worker()
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_dot)
        self._pulse_timer.start(900)

    def _ui(self):
        self.setWindowTitle("Sagacious")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Window
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(370, 660)
        self.setMinimumWidth(320)
        self.setMaximumWidth(440)

        shell = QWidget(); self.setCentralWidget(shell)
        shell.setStyleSheet("background: transparent;")
        shell_lay = QVBoxLayout(shell); shell_lay.setContentsMargins(10,10,10,10)

        self.glass = QWidget(); self.glass.setObjectName("glass")
        self.glass.setStyleSheet(f"""
            QWidget#glass {{
                background: {BG}; border-radius: 18px; border: 1px solid {BORDER};
            }}
        """)
        shell_lay.addWidget(self.glass)

        self.stack = QStackedWidget()
        glass_lay = QVBoxLayout(self.glass)
        glass_lay.setContentsMargins(0,0,0,0); glass_lay.setSpacing(0)
        glass_lay.addWidget(self.stack)

        # ── Main page ──────────────────────────────────────────────────────────
        main_page = QWidget(); main_page.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(main_page); lay.setContentsMargins(18,16,18,14); lay.setSpacing(0)

        # Title bar / drag handle
        titlebar = QWidget(); titlebar.setStyleSheet("background:transparent;"); titlebar.setFixedHeight(44)
        tb = QHBoxLayout(titlebar); tb.setContentsMargins(0,0,0,0); tb.setSpacing(8)

        bulb = QLabel("💡"); bulb.setStyleSheet("background:transparent;border:none;font-size:18px;")
        tb.addWidget(bulb); tb.addSpacing(4)

        logo = QLabel("Sagacious"); logo.setStyleSheet(f"color:{TEXT};background:transparent;")
        logo.setFont(QFont("SF Pro Display", 14, QFont.Weight.DemiBold))
        tb.addWidget(logo); tb.addStretch()

        self.status_pill = QFrame()
        self.status_pill.setStyleSheet(f"QFrame{{background:rgba(50,215,75,18);border-radius:10px;border:1px solid rgba(50,215,75,40);}}")
        pill_lay = QHBoxLayout(self.status_pill); pill_lay.setContentsMargins(8,4,10,4); pill_lay.setSpacing(5)
        self.dot_lbl = QLabel("●"); self.dot_lbl.setStyleSheet(f"color:{GREEN};font-size:8px;background:transparent;border:none;")
        self.status_lbl = QLabel("Watching"); self.status_lbl.setStyleSheet(f"color:{GREEN};font-size:11px;font-weight:500;background:transparent;border:none;")
        pill_lay.addWidget(self.dot_lbl); pill_lay.addWidget(self.status_lbl)
        tb.addWidget(self.status_pill); tb.addSpacing(6)

        self.pause_btn = QPushButton("⏸"); self.pause_btn.setFixedSize(28,28)
        self.pause_btn.setStyleSheet(f"QPushButton{{background:{GLASS};color:{SOFT};font-size:13px;border:1px solid {BORD2};border-radius:8px;}} QPushButton:hover{{background:{GLASS2};color:{YELLOW};}}")
        self.pause_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.pause_btn.clicked.connect(self._toggle_pause); tb.addWidget(self.pause_btn)

        gear = QPushButton("⚙"); gear.setFixedSize(28,28)
        gear.setStyleSheet(f"QPushButton{{background:{GLASS};color:{SOFT};font-size:14px;border:1px solid {BORD2};border-radius:8px;}} QPushButton:hover{{background:{GLASS2};color:{TEXT};}}")
        gear.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        gear.clicked.connect(self._show_settings); tb.addWidget(gear)
        lay.addWidget(titlebar)

        self.provider_badge = QLabel(self._badge_text())
        self.provider_badge.setStyleSheet(f"color:{MUTED};font-size:10px;background:transparent;letter-spacing:0.2px;")
        lay.addWidget(self.provider_badge); lay.addSpacing(8)

        saved_mode = self.config.get("mode","events")
        self.mode_ctrl = SegmentedControl(saved_mode)
        self.mode_ctrl.changed.connect(self._on_mode_change)
        lay.addWidget(self.mode_ctrl); lay.addSpacing(8)

        div = QFrame(); div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"background:{BORD2};border:none;max-height:1px;"); lay.addWidget(div)
        lay.addSpacing(8)

        SCROLL_CSS = f"""
            QScrollArea{{border:none;background:transparent;}}
            QScrollBar:vertical{{width:3px;background:transparent;margin:4px 0;}}
            QScrollBar::handle:vertical{{background:rgba(255,255,255,25);border-radius:1px;min-height:20px;}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0px;}}
        """
        self.mode_scrolls = {}
        self.mode_containers = {}
        self.mode_layouts = {}
        self.mode_bubbles = {}
        self.feed_stack = QStackedWidget()
        self.feed_stack.setStyleSheet("background:transparent;")
        for i, mode in enumerate(["events", "process", "overview"]):
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet(SCROLL_CSS)
            bc = QWidget(); bc.setStyleSheet("background:transparent;")
            bl = QVBoxLayout(bc); bl.setSpacing(8); bl.setContentsMargins(0,0,4,0); bl.addStretch()
            scroll.setWidget(bc)
            self.mode_scrolls[mode] = scroll
            self.mode_containers[mode] = bc
            self.mode_layouts[mode] = bl
            self.mode_bubbles[mode] = []
            self.feed_stack.addWidget(scroll)
        self._mode_stack_index = {"events": 0, "process": 1, "overview": 2}
        lay.addWidget(self.feed_stack); lay.addSpacing(6)

        self.foot = QLabel("Open Terminal to start monitoring")
        self.foot.setStyleSheet(f"color:{MUTED};font-size:11px;background:transparent;")
        self.foot.setAlignment(Qt.AlignmentFlag.AlignCenter); lay.addWidget(self.foot); lay.addSpacing(8)

        chat_row = QHBoxLayout(); chat_row.setSpacing(8)
        self.chat_input = QLineEdit(); self.chat_input.setPlaceholderText("Ask anything about your terminal…")
        self.chat_input.setStyleSheet(INPUT_STYLE); self.chat_input.setFixedHeight(38)
        self.chat_input.returnPressed.connect(self._send_chat)
        self.send_btn = QPushButton("↑"); self.send_btn.setFixedSize(38,38)
        self.send_btn.setStyleSheet(f"""
            QPushButton{{background:{ACCENT};color:white;font-size:16px;font-weight:700;border-radius:19px;border:none;}}
            QPushButton:hover{{background:#7577f5;}} QPushButton:pressed{{background:#4f51d4;}}
            QPushButton:disabled{{background:rgba(99,102,241,70);}}
        """)
        self.send_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.send_btn.clicked.connect(self._send_chat)
        chat_row.addWidget(self.chat_input); chat_row.addWidget(self.send_btn)
        lay.addLayout(chat_row)

        # ── Settings page ──────────────────────────────────────────────────────
        settings_wrap = QWidget(); settings_wrap.setStyleSheet("background:transparent;")
        sw_lay = QVBoxLayout(settings_wrap); sw_lay.setContentsMargins(0,0,0,0); sw_lay.setSpacing(0)

        settings_bar = QWidget(); settings_bar.setFixedHeight(52); settings_bar.setStyleSheet("background:transparent;")
        sb_lay = QHBoxLayout(settings_bar); sb_lay.setContentsMargins(18,0,18,0)
        back = QPushButton("← Back")
        back.setStyleSheet(f"QPushButton{{background:transparent;color:{ACCENT};font-size:13px;border:none;font-weight:500;}} QPushButton:hover{{color:#818cf8;}}")
        back.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        s_title = QLabel("Settings"); s_title.setStyleSheet(f"color:{TEXT};font-size:14px;font-weight:600;background:transparent;")
        sb_lay.addWidget(back); sb_lay.addStretch(); sb_lay.addWidget(s_title); sb_lay.addStretch()
        pad = QLabel(); pad.setFixedWidth(60); sb_lay.addWidget(pad)
        sw_lay.addWidget(settings_bar)

        sdiv = QFrame(); sdiv.setFrameShape(QFrame.Shape.HLine)
        sdiv.setStyleSheet(f"background:{BORD2};border:none;max-height:1px;"); sw_lay.addWidget(sdiv)

        self.settings_panel = SettingsPanel(self.config)
        sw_lay.addWidget(self.settings_panel)
        self.settings_panel.saved.connect(self._on_settings_saved)

        self.stack.addWidget(main_page)
        self.stack.addWidget(settings_wrap)
        self.feed_stack.setCurrentIndex(self._mode_stack_index.get(saved_mode, 0))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def _pulse_dot(self):
        self._dot_on = not self._dot_on
        self.dot_lbl.setVisible(self._dot_on)

    def _badge_text(self):
        p = self.config.get("provider","ollama")
        if p == "claude": return "Claude Haiku · cloud"
        m = self.config.get("ollama_model","llama3.2")
        return f"{m} · local · private"

    def _toggle_pause(self):
        is_paused = not self.worker.paused
        self.worker.set_paused(is_paused)
        if is_paused:
            self.pause_btn.setText("▶")
            self._pulse_timer.stop(); self.dot_lbl.setVisible(True)
        else:
            self.pause_btn.setText("⏸")
            self._pulse_timer.start(900)

    def _on_mode_change(self, mode):
        self.config["mode"] = mode
        save_config(self.config)
        self.worker.set_mode(mode)
        self.feed_stack.setCurrentIndex(self._mode_stack_index[mode])

    def _show_settings(self):
        self.stack.setCurrentIndex(1)

    def _on_settings_saved(self, cfg):
        self.config = cfg
        self.provider_badge.setText(self._badge_text())
        self.worker.update_config(cfg)
        self.stack.setCurrentIndex(0)

    def _add(self, text, mode=None, question=None):
        p = self.config.get("provider","ollama")
        label = p if p == "claude" else self.config.get("ollama_model","llama3.2")
        b = Bubble(text, label, question=question)
        target_mode = mode if mode in self.mode_layouts else self.config.get("mode", "events")
        bl = self.mode_layouts[target_mode]
        bubbles = self.mode_bubbles[target_mode]
        scroll = self.mode_scrolls[target_mode]
        bl.insertWidget(bl.count()-1, b); bubbles.append(b)
        if len(bubbles) > MAX_BUBBLES:
            old = bubbles.pop(0); bl.removeWidget(old); old.deleteLater()
        QTimer.singleShot(60, lambda: scroll.verticalScrollBar().setValue(
            scroll.verticalScrollBar().maximum()))

    def _send_chat(self):
        q = self.chat_input.text().strip()
        if not q: return
        self.chat_input.clear(); self.chat_input.setEnabled(False); self.send_btn.setEnabled(False)
        terminal_text = self.worker.last_text if hasattr(self.worker, "last_text") else ""
        self._chat_worker = ChatWorker(q, terminal_text, self.config)
        self._chat_worker.reply_ready.connect(self._on_chat_reply)
        self._chat_worker.start()

    def _on_chat_reply(self, question, answer):
        self._add(answer, mode=self.config.get("mode","events"), question=question)
        self.chat_input.setEnabled(True); self.send_btn.setEnabled(True); self.chat_input.setFocus()

    def _status(self, s):
        states = {
            "watching":    (GREEN,  "rgba(50,215,75,18)",  "rgba(50,215,75,40)",  "Watching",       "Monitoring your terminal"),
            "thinking":    (YELLOW, "rgba(255,214,10,18)", "rgba(255,214,10,40)", "Thinking",       "Working it out…"),
            "paused":      (MUTED,  GLASS,                 BORD2,                 "Paused",         "Press ▶ to resume"),
            "no_terminal": (RED,    "rgba(255,69,58,18)",  "rgba(255,69,58,40)",  "No Terminal",    "Open Terminal to begin"),
            "no_ollama":   (RED,    "rgba(255,69,58,18)",  "rgba(255,69,58,40)",  "Ollama offline", "Open Ollama.app"),
            "no_key":      (RED,    "rgba(255,69,58,18)",  "rgba(255,69,58,40)",  "No API key",     "Add key in Settings ⚙"),
        }
        color, pill_bg, pill_border, lbl, foot = states.get(s, states["watching"])
        self.status_pill.setStyleSheet(f"QFrame{{background:{pill_bg};border-radius:10px;border:1px solid {pill_border};}}")
        self.dot_lbl.setStyleSheet(f"color:{color};font-size:8px;background:transparent;border:none;")
        self.status_lbl.setStyleSheet(f"color:{color};font-size:11px;font-weight:500;background:transparent;border:none;")
        self.status_lbl.setText(lbl); self.foot.setText(foot)

    def _start_worker(self):
        self.worker = CaptureWorker(self.config)
        self.worker.new_explanation.connect(lambda text, mode: self._add(text, mode=mode))
        self.worker.status_changed.connect(self._status)
        self.worker.start()

    def closeEvent(self, e):
        self.worker.stop(); self.worker.wait(); e.accept()


def _activate_app():
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Sagacious")
    w = ExplainerWindow(); w.show()
    _activate_app()
    sys.exit(app.exec())

if __name__ == "__main__": main()
