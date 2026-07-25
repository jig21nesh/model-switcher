"""Render a terminal replay as an animated GIF for the README.

Deliberately a replay of *captured real output*, not a mock-up: `capture_demo.sh` runs the
real installer and CLI in a sandbox and writes the transcripts this reads. Nothing here
invents output — it only types it back with a cursor.

Build-time only. Needs Pillow, which is not a project dependency; the generated GIFs are
committed so nobody has to install anything to build or test model-switcher.

Usage: python tools/make_demo_gif.py <scene> --capture DIR --out FILE
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - build tool
    sys.exit("this needs Pillow: pip install Pillow")

FONT_CANDIDATES = ("/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf")
FONT_SIZE = 15
COLS, ROWS = 104, 24
PAD, TITLE_H = 18, 30

BG = (23, 25, 33)
CHROME = (33, 36, 46)
FG = (198, 203, 215)
DIM = (108, 114, 132)
GREEN = (126, 200, 130)
CYAN = (108, 187, 204)
ORANGE = (224, 154, 92)
YELLOW = (214, 190, 108)
WHITE = (238, 241, 247)
RED = (232, 118, 112)

TYPE_CHARS_PER_FRAME = 4
MS_TYPE, MS_LINE, MS_PAUSE, MS_HOLD = 45, 70, 700, 2600

# Terminal output is plain text; colour is applied here by rule so the replay reads clearly.
RULES = (
    (re.compile(r"COMPLEX|heavy-task-[\w-]+"), ORANGE),
    (re.compile(r"MODERATE|mid-task-[\w-]+"), YELLOW),
    (re.compile(r"answered in-session|in-session"), CYAN),
    (re.compile(r"saved \$[\d.,]+ \(\d+%\)|with terms|\+\d+\.\d+"), GREEN),
    (re.compile(r"routing off|no rate:|-\d+\.\d+"), RED),
    (re.compile(r"\$[\d.,]+|\d+\.\d+%|\b\d+/10\b"), WHITE),
)


def load_font():
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, FONT_SIZE)
    sys.exit("no monospace font found")


class Terminal:
    """Accumulates styled lines and renders frames, scrolling like a real terminal."""

    def __init__(self, font):
        self.font = font
        self.char_w = font.getbbox("M")[2] - font.getbbox("M")[0]
        self.line_h = FONT_SIZE + 7
        self.width = COLS * self.char_w + PAD * 2
        self.height = ROWS * self.line_h + PAD * 2 + TITLE_H
        self.lines: list[list[tuple[str, tuple]]] = []
        self.frames: list[Image.Image] = []
        self.durations: list[int] = []

    # -- content -------------------------------------------------------------
    def _wrap(self, text: str) -> list[str]:
        return [text[i:i + COLS] for i in range(0, len(text), COLS)] or [""]

    def add_output(self, text: str) -> None:
        for chunk in self._wrap(text.rstrip("\n")):
            self.lines.append(self._style(chunk))

    def _style(self, text: str) -> list[tuple[str, tuple]]:
        base = DIM if text.strip().startswith("#") else FG
        spans = [[ch, base] for ch in text]
        for pattern, colour in RULES:
            for match in pattern.finditer(text):
                for i in range(match.start(), match.end()):
                    spans[i][1] = colour
        merged: list[tuple[str, tuple]] = []
        for ch, colour in spans:
            if merged and merged[-1][1] == colour:
                merged[-1] = (merged[-1][0] + ch, colour)
            else:
                merged.append((ch, colour))
        return merged

    # -- frames --------------------------------------------------------------
    def _render(self, cursor: bool = False) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, self.width, TITLE_H], fill=CHROME)
        for i, colour in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            x = 16 + i * 18
            draw.ellipse([x, TITLE_H // 2 - 5, x + 10, TITLE_H // 2 + 5], fill=colour)
        draw.text((self.width // 2 - 60, TITLE_H // 2 - 8), "model-switcher", font=self.font, fill=DIM)

        visible = self.lines[-ROWS:]
        for row, spans in enumerate(visible):
            x, y = PAD, TITLE_H + PAD + row * self.line_h
            for text, colour in spans:
                draw.text((x, y), text, font=self.font, fill=colour)
                x += len(text) * self.char_w
            if cursor and row == len(visible) - 1:
                draw.rectangle([x + 1, y + 2, x + self.char_w, y + FONT_SIZE + 2], fill=GREEN)
        return image

    def snap(self, ms: int, cursor: bool = False) -> None:
        self.frames.append(self._render(cursor))
        self.durations.append(ms)

    def type_command(self, command: str) -> None:
        self.lines.append([("$ ", GREEN)])
        for end in range(0, len(command) + 1, TYPE_CHARS_PER_FRAME):
            self.lines[-1] = [("$ ", GREEN), (command[:end], WHITE)]
            self.snap(MS_TYPE, cursor=True)
        self.lines[-1] = [("$ ", GREEN), (command, WHITE)]
        self.snap(MS_PAUSE, cursor=True)

    def emit(self, text: str, ms: int = MS_LINE) -> None:
        for line in text.split("\n"):
            self.add_output(line)
            self.snap(ms)

    def save(self, out: Path) -> None:
        self.durations[-1] = MS_HOLD
        quantised = [f.quantize(colors=32, method=Image.MEDIANCUT) for f in self.frames]
        quantised[0].save(
            out, save_all=True, append_images=quantised[1:],
            duration=self.durations, loop=0, optimize=True, disposal=2,
        )
        _shrink(out)


def _shrink(path: Path) -> None:
    """Squeeze the GIF with ImageMagick when it is available. Optional, never fatal."""
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        return
    tmp = path.with_suffix(".opt.gif")
    result = subprocess.run(
        [magick, str(path), "-layers", "OptimizeTransparency", "-layers", "OptimizeFrame", str(tmp)],
        capture_output=True,
    )
    if result.returncode == 0 and tmp.exists() and 0 < tmp.stat().st_size < path.stat().st_size:
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)


def read(capture: Path, name: str) -> str:
    return (capture / name).read_text(encoding="utf-8").rstrip("\n")


def scene_install(term: Terminal, capture: Path) -> None:
    term.emit("# One-time setup. After this you never need the repo again.", MS_PAUSE)
    term.type_command("git clone https://github.com/jig21nesh/model-switcher.git")
    term.emit("Cloning into 'model-switcher'... done.")
    term.type_command("cd model-switcher && ./install.sh")
    term.emit(read(capture, "install.txt"))
    term.snap(MS_PAUSE)
    term.emit("")
    term.emit("# Everything now lives in ~/.claude — including the CLI itself.", MS_PAUSE)
    term.type_command("ls ~/.claude/model-switcher/")
    term.emit(read(capture, "listing.txt"))
    term.snap(MS_HOLD)


def scene_usage(term: Terminal, capture: Path) -> None:
    term.emit("# See where a prompt routes, without spending a token.", MS_PAUSE)
    term.type_command('model-switcher explain "refactor the auth module and migrate the schema"')
    term.emit(read(capture, "explain1.txt"))
    term.snap(MS_PAUSE)
    term.type_command('model-switcher explain "what does this function do?"')
    term.emit(read(capture, "explain2.txt"))
    term.snap(MS_PAUSE)
    term.emit("")
    term.emit("# Tune the router on your own history. Nothing leaves the machine.", MS_PAUSE)
    term.type_command("model-switcher learn")
    term.emit(read(capture, "learn_trimmed.txt"))
    term.snap(MS_PAUSE)
    term.emit("")
    term.emit("# And the statusline prices every turn offline:", MS_PAUSE)
    term.emit(read(capture, "statusline.txt"))
    term.snap(MS_HOLD)


SCENES = {"install": scene_install, "usage": scene_usage}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", choices=sorted(SCENES))
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    term = Terminal(load_font())
    SCENES[args.scene](term, args.capture)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    term.save(args.out)
    total = sum(term.durations) / 1000
    print(f"{args.out}: {len(term.frames)} frames, {total:.1f}s, "
          f"{term.width}x{term.height}, {args.out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
