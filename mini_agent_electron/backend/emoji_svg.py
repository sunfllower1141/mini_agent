"""
emoji_svg.py — Replace emoji characters with minimalistic inline SVG icons.

All SVGs are 14×14px, use currentColor for stroke, and follow the same
style as the existing tool-icon SVGs in App.jsx.
"""

import re

# ---------------------------------------------------------------------------
# SVG icon library — each is a self-contained 14×14 inline SVG
# ---------------------------------------------------------------------------

# Use a common prefix for the SVG wrapper to keep the map compact
_SVG_OPEN = '<svg class="emoji-icon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
_SVG_CLOSE = '</svg>'

def _svg(paths: str) -> str:
    """Wrap one or more <path> elements in a standard SVG shell."""
    return f'{_SVG_OPEN}{paths}{_SVG_CLOSE}'


# Map of emoji character → inline SVG HTML
# Covers the most common emojis that appear in agent output.
EMOJI_MAP: dict[str, str] = {
    # -- Status / result icons --
    '\u2705': _svg('<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>'),   # ✅ check-circle
    '\u2714': _svg('<polyline points="20 6 9 17 4 12"/>'),                                                       # ✔️ check
    '\u2713': _svg('<polyline points="20 6 9 17 4 12"/>'),                                                       # ✓ check
    '\u274c': _svg('<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>'),  # ❌ x-circle
    '\u274e': _svg('<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>'),  # ❎ cross mark
    '\u2717': _svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),                # ✗ ballot x
    '\u2718': _svg('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),                # ✘ ballot x bold
    '\u26a0': _svg('<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),  # ⚠ warning
    '\u26a0\ufe0f': _svg('<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),  # ⚠️

    # -- Info / idea --
    '\u2139': _svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>'),  # ℹ
    '\u2139\ufe0f': _svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>'),  # ℹ️
    '\U0001f4a1': _svg('<path d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 1 1 7.072 0l-.548.547A3.374 3.374 0 0 0 14 18.469V19a2 2 0 1 1-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>'),  # 💡 lightbulb

    # -- Files / folders --
    '\U0001f4c1': _svg('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),  # 📁 folder
    '\U0001f4c2': _svg('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M22 11H2"/>'),  # 📂 folder-open
    '\U0001f4c4': _svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'),  # 📄 file
    '\U0001f4c3': _svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>'),  # 📃 document-text
    '\U0001f4dd': _svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>'),  # 📝 edit
    '\U0001f4ce': _svg('<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'),  # 📎 paperclip

    # -- Tools / actions --
    '\U0001f527': _svg('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),  # 🔧 wrench
    '\U0001f528': _svg('<path d="M15 12l-8.5 8.5a2.12 2.12 0 0 1-3-3L12 9"/><path d="M17.64 15 22 10.64"/><path d="M20.91 11.7a2 2 0 0 0-2.82-3.53L6.2 20a2 2 0 1 0 2.82 3.53L20.91 11.7Z"/>'),  # 🔨 hammer
    '\U0001f50d': _svg('<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>'),  # 🔍 search
    '\U0001f50e': _svg('<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/><line x1="8" y1="11" x2="14" y2="11"/>'),  # 🔎 search-plus
    '\U0001f5d1': _svg('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),  # 🗑 trash
    '\U0001f4be': _svg('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>'),  # 💾 save
    '\U0001f512': _svg('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),  # 🔒 lock
    '\U0001f513': _svg('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>'),  # 🔓 unlock
    '\U0001f6e0': _svg('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/><path d="M7 15l-2 2"/><path d="M9 17l-2 2"/><path d="M5 21l-2 2"/>'),  # 🛠 hammer-and-wrench

    # -- Navigation / transport --
    '\U0001f680': _svg('<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4l3.5-2.5"/><path d="M15 4.23V9l-3.5 2.5"/><path d="M16 16s-1.5 2-3.5 2"/><path d="M8 16s1.5 2 3.5 2"/>'),  # 🚀 rocket
    '\u23f3': _svg('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),  # ⏳ hourglass

    # -- Arrows --
    '\u2b05': _svg('<line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>'),  # ⬅ left
    '\u27a1': _svg('<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>'),  # ➡ right
    '\u2b06': _svg('<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>'),  # ⬆ up
    '\u2b07': _svg('<line x1="12" y1="5" x2="12" y2="19"/><polyline points="5 12 12 19 19 12"/>'),  # ⬇ down
    '\U0001f500': _svg('<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>'),  # 🔀 shuffle
    '\U0001f504': _svg('<polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>'),  # 🔄 refresh

    # -- Shapes / indicators --
    '\U0001f7e2': _svg('<circle cx="12" cy="12" r="10"/>'),  # 🟢 green circle
    '\U0001f534': _svg('<circle cx="12" cy="12" r="10"/>'),  # 🔴 red circle
    '\U0001f7e1': _svg('<circle cx="12" cy="12" r="10"/>'),  # 🟡 yellow circle
    '\U0001f7e0': _svg('<circle cx="12" cy="12" r="10"/>'),  # 🟠 orange circle
    '\U0001f7e3': _svg('<circle cx="12" cy="12" r="10"/>'),  # 🟣 purple circle
    '\u26aa': _svg('<circle cx="12" cy="12" r="10"/>'),      # ⚪ white circle
    '\u26ab': _svg('<circle cx="12" cy="12" r="10"/>'),      # ⚫ black circle

    # -- Plus / minus --
    '\u2795': _svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>'),  # ➕ plus-circle
    '\u2796': _svg('<circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/>'),  # ➖ minus-circle

    # -- Misc --
    '\u2b50': _svg('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'),  # ⭐ star
    '\U0001f31f': _svg('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'),  # 🌟 glowing star
    '\U0001f4a5': _svg('<path d="M12 2l2 8 8 2-8 2-2 8-2-8-8-2 8-2z"/>'),  # 💥 burst
    '\u26a1': _svg('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),  # ⚡ zap
    '\u26a1\ufe0f': _svg('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),  # ⚡️
    '\U0001f517': _svg('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),  # 🔗 link
    '\U0001f4cc': _svg('<path d="M12 2v7"/><path d="M9 5h6"/><circle cx="12" cy="17" r="4"/>'),  # 📌 pin
    '\U0001f4cb': _svg('<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/>'),  # 📋 clipboard
    '\U0001f4ca': _svg('<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>'),  # 📊 chart-bar
    '\U0001f9e0': _svg('<path d="M12 2a10 10 0 1 0 0 20 10 10 0 1 0 0-20z"/><path d="M12 6v2M12 16v2M8 12H6M18 12h-2M9.17 8.17l1.41 1.41M13.42 14.42l1.41 1.41M14.83 8.17l-1.41 1.41M10.58 14.42l-1.41 1.41"/>'),  # 🧠 brain
    '\U0001f41b': _svg('<circle cx="12" cy="12" r="10"/><path d="M8 8l8 8M8 16l8-8M5 12h2M17 12h2M12 5v2M12 17v2"/>'),  # 🐛 bug
    '\U0001f389': _svg('<path d="M12 3l2 5 5 1-3 4 1 5-5-3-5 3 1-5-3-4 5-1z"/><path d="M2 7l2 2M20 7l-2 2M7 2l1 2M16 2l-1 2M7 20l2-2M15 20l2-2M2 17l2-2M20 17l-2-2"/>'),  # 🎉 party
    '\U0001f44d': _svg('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" fill="currentColor" fill-opacity="0.15"/>'),  # 👍 thumbs-up
    '\U0001f44e': _svg('<path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3" fill="currentColor" fill-opacity="0.15"/>'),  # 👎 thumbs-down
    '\U0001f3d7': _svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'),  # 🏗 construction
    '\U0001f9f9': _svg('<path d="M6 3h12l2 6l-2 12H6L4 9z"/><line x1="6" y1="9" x2="18" y2="9"/><line x1="12" y1="3" x2="12" y2="9"/><line x1="4" y1="9" x2="2" y2="15"/><line x1="20" y1="9" x2="22" y2="15"/>'),  # 🧹 broom
    '\U0001f9ea': _svg('<path d="M6 2v4l-4 8h20l-4-8V2"/><path d="M6 6h12"/><line x1="8" y1="10" x2="8" y2="18"/><line x1="12" y1="10" x2="12" y2="18"/><line x1="16" y1="10" x2="16" y2="18"/>'),  # 🧪 test-tube

    # -- Exclamation / emphasis --
    '\u2757': _svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="17" x2="12" y2="9"/><line x1="12" y1="7" x2="12.01" y2="7"/>'),  # ❗ exclamation
    '\u203c': _svg('<circle cx="12" cy="12" r="10"/><line x1="10" y1="17" x2="10" y2="9"/><line x1="10" y1="7" x2="10.01" y2="7"/><line x1="14" y1="17" x2="14" y2="9"/><line x1="14" y1="7" x2="14.01" y2="7"/>'),  # ‼ double exclamation
    '\u2755': _svg('<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="14"/><line x1="12" y1="16" x2="12.01" y2="16"/>'),  # ❕ white exclamation
    '\u2754': _svg('<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),  # ❔ question
    '\u2753': _svg('<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),  # ❓ question-filled

    # -- Symbols --
    '\u00a9': _svg('<circle cx="12" cy="12" r="10"/><path d="M14.5 9.5a3 3 0 1 0 0 5"/>'),  # © copyright
    '\u00ae': _svg('<circle cx="12" cy="12" r="10"/><path d="M10 8h3a3 3 0 0 1 0 6h-3V8z"/><line x1="14" y1="14" x2="16" y2="17"/>'),  # ® registered
    '\u2122': _svg('<path d="M6 4h4v12"/><path d="M10 4l3 6 3-6h2v12"/><path d="M10 10h6"/>'),  # ™ trademark

    # -- Hearts / reactions --
    '\u2764': _svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>'),  # ❤ heart
    '\u2764\ufe0f': _svg('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>'),  # ❤️

    # -- Fire / energy --
    '\U0001f525': _svg('<path d="M12 2C8.5 6 4 8 4 13a8 8 0 0 0 16 0c0-5-4.5-7-8-11z"/>'),  # 🔥 fire

    # -- Spinner / loading --
    '\U0001f300': _svg('<line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/>'),  # 🌀 cyclone/loading-spinner
}


# ---------------------------------------------------------------------------
# Emoji detection regex
# ---------------------------------------------------------------------------

# Build a regex that matches any emoji we have a mapping for.
# Sort by length (longest first) so multi-codepoint emojis match before
# their constituent parts.
_ESCAPED = [re.escape(emoji) for emoji in sorted(EMOJI_MAP.keys(), key=len, reverse=True)]
_EMOJI_RE = re.compile('|'.join(_ESCAPED)) if _ESCAPED else re.compile(r'(?!)')


def replace_emojis(text: str) -> str:
    """Replace all known emojis in `text` with their SVG equivalents."""
    if not text or not _ESCAPED:
        return text
    return _EMOJI_RE.sub(lambda m: EMOJI_MAP.get(m.group(0), m.group(0)), text)


# Also export a broader emoji stripper for any emoji not in our map.
# This uses a Unicode property regex to catch all emoji characters.
_FULL_EMOJI_RE = re.compile(
    '[\U0001f300-\U0001f9ff'   # Miscellaneous Symbols, Emoticons, Supplemental, Transport
    '\U0001fa00-\U0001fa6f'    # Chess symbols
    '\U0001fa70-\U0001faff'    # Symbols extended-A
    '\U00002600-\U000027bf'    # Misc symbols (includes some non-emoji)
    '\U0000fe00-\U0000fe0f'    # Variation selectors
    '\U0001f000-\U0001f02f'    # Mahjong, Domino
    '\U0001f0a0-\U0001f0ff'    # Playing cards
    '\U0001f100-\U0001f64f'    # Enclosed, Emoticons
    '\U0001f680-\U0001f6ff'    # Transport
    '\U0001f780-\U0001f7ff'    # Geometric shapes extended
    '\U0001f900-\U0001f9ff'    # Supplemental symbols
    '\U0001fae0-\U0001faff'    # Symbols extended-A
    '\U0000231a-\U0000231b'    # Watch, hourglass
    '\U000023e9-\U000023f3'    # VCR, hourglass
    '\U000023f8-\U000023fa'    # VCR
    '\U00002b06-\U00002b07'    # Arrows
    '\U00002764-\U00002765'    # Heart
    '\U00002708-\U0000270f'    # Dingbats
    '\U0000274c-\U0000275c'    # More dingbats
    '\U0000fe0f'              # Variation selector-16
    ']+',
    re.UNICODE,
)


def strip_remaining_emojis(text: str) -> str:
    """Remove any emoji characters not handled by replace_emojis."""
    return _FULL_EMOJI_RE.sub('', text)


def clean_text(text: str) -> str:
    """Pass-through — emoji-to-SVG conversion removed; emojis render natively."""
    return text
