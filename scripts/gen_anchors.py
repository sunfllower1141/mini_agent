#!/usr/bin/env python3
"""Generate core/_anchor_words.py from core/.hash_anchors (Dirac anchor dictionary).

Run this whenever core/.hash_anchors changes to regenerate the Python tuple.
"""
from __future__ import annotations

import os
import sys

def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hash_anchors_path = os.path.join(repo_root, "core", ".hash_anchors")
    anchor_words_path = os.path.join(repo_root, "core", "_anchor_words.py")

    # Read words from Dirac's .hash_anchors file
    with open(hash_anchors_path, encoding="utf-8") as f:
        words = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    # Write as a Python tuple
    with open(anchor_words_path, "w", encoding="utf-8") as f:
        f.write('"""1721 random words for stable anchors (from Dirac anchor dictionary)."""\n')
        f.write("from __future__ import annotations\n\n")
        f.write("_ANCHOR_WORDS: tuple[str, ...] = (\n")
        for w in words:
            f.write(f'    "{w}",\n')
        f.write(")\n")

    print(f"Wrote {len(words)} words to {anchor_words_path}")

if __name__ == "__main__":
    main()
