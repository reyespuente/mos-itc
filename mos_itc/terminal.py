"""Curses widgets shared by menus, forms, reviews and reports."""

import curses
import json
import math
import os
from pathlib import Path
import re
import textwrap
import time

from .core import VERSION

ASSETS = Path(__file__).resolve().parent.parent / "assets"
INSTITUTE = "Instituto Tecnológico de Celaya"
DISPLAY_AUTHOR = "Reyes Puente"
WORDMARK = r""" __  __  ___  ____    ___ _____ ____
|  \/  |/ _ \/ ___|  |_ _|_   _/ ___|
| |\/| | | | \___ \   | |  | || |
| |  | | |_| |___) |  | |  | || |___
|_|  |_|\___/|____/  |___| |_| \____|"""


class Cancelled(Exception):
    """Return to the previous screen without reporting an error."""


def clean(value):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(value))


def wrapped(text, width):
    lines = []
    for line in clean(text).expandtabs(4).splitlines():
        lines.extend(
            textwrap.wrap(line, max(1, width), replace_whitespace=False) or [""]
        )
    return lines or [""]


class Terminal:
    def __init__(self, window, translate, preview=False):
        self.win = window
        self.t = translate
        self.preview = preview
        self.positions = {}
        self.status = self.t("ui_ready")
        self.status_error = False
        self.title = "main_menu"
        self.context = {}
        self.original_colors = {}
        self.logo = json.loads((ASSETS / "itc-logo.json").read_text())
        self.ink = {}
        self.win.keypad(True)
        curses.curs_set(0)
        curses.set_escdelay(30)
        if curses.has_colors() and not os.environ.get("NO_COLOR"):
            curses.start_color()
            curses.use_default_colors()
            colors = [
                "#111923",
                "#e6edf3",
                "#77899e",
                "#1b396a",
                "#117533",
                "#ffffff",
                "#008137",
                "#d20b14",
                "#ffdf00",
                "#88877b",
            ]
            indices = [235, 255, 245, 24, 28, 15, 28, 160, 220, 245]
            # Use standard palette slots: redefining extended slots renders
            # inconsistently across terminal emulators, including Konsole.
            if curses.COLORS < 256:
                indices = [
                    curses.COLOR_BLACK,
                    curses.COLOR_WHITE,
                    curses.COLOR_WHITE,
                    curses.COLOR_BLUE,
                    curses.COLOR_GREEN,
                    curses.COLOR_WHITE,
                    curses.COLOR_GREEN,
                    curses.COLOR_RED,
                    curses.COLOR_YELLOW,
                    curses.COLOR_WHITE,
                ]
            for pair, foreground, background in [
                (1, indices[1], indices[0]),
                (2, indices[2], indices[0]),
                (3, indices[1], indices[3]),
                (4, indices[8], indices[0]),
                (5, indices[6], indices[0]),
                (6, indices[7], indices[0]),
            ]:
                curses.init_pair(pair, foreground, background)
            curses.init_pair(
                12, 114 if curses.COLORS >= 256 else curses.COLOR_GREEN, indices[0]
            )
            for pair, label, index in zip(range(7, 12), "WGRYH", indices[5:]):
                curses.init_pair(pair, index, indices[0])
                self.ink[label] = curses.color_pair(pair)
            self.win.bkgd(" ", curses.color_pair(1))

    def close(self):
        for index, rgb in self.original_colors.items():
            curses.init_color(index, *rgb)

    def style(self, pair=1, bold=False):
        value = (
            curses.color_pair(pair)
            if curses.has_colors() and not os.environ.get("NO_COLOR")
            else 0
        )
        return value | (curses.A_BOLD if bold else 0)

    def resource_style(self, text):
        value = re.search(r"\]\s*([0-9.]+)%", text)
        if value:
            percent = float(value[1])
            return self.style(6 if percent >= 95 else 4 if percent >= 85 else 12)
        return self.style(12 if any(char in text for char in "█▇▆▅▄▃▂▁░") else 1)

    def put(self, y, x, text, width=None, style=None):
        h, w = self.win.getmaxyx()
        if y < 0 or x < 0 or y >= h or x >= w - 1:
            return
        limit = min(width if width is not None else w - x - 1, w - x - 1)
        if limit > 0:
            try:
                self.win.addnstr(
                    y, x, clean(text).replace("\n", " "), limit, style or 0
                )
            except curses.error:
                # Resizing can invalidate coordinates between getmaxyx and drawing.
                pass

    def frame(self, title, footer=None):
        self.title = title
        self.win.erase()
        h, w = self.win.getmaxyx()
        self.put(1, 3, "Manage Your Own Server", style=self.style(5, True))
        mode = self.t("ui_preview_short") if self.preview else os.uname().nodename
        line = f"Tool made by ITC - Reyes Puente | {mode} | {self.context.get('public') or '—'} | {VERSION}"
        if self.context.get("update"):
            line += (
                " | "
                + self.t("app_update_available")
                + " "
                + self.context["update"]["tag"]
            )
        # The wordmark is shown only when both it and the left title fit.
        if w >= 110:
            left = w - 45
            for dy, text in enumerate(WORDMARK.splitlines()):
                self.put(dy, left, text, 42, self.style(5, True))
            self.put(3, 3, line, max(1, left - 5), self.style(2))
            if len(line) > left - 5:
                self.put(4, 3, line[left - 5 :], max(1, left - 5), self.style(2))
        else:
            for dy, text in enumerate(textwrap.wrap(line, max(1, w - 7))[:3]):
                self.put(2 + dy, 3, text, style=self.style(2))
        self.put(5, 3, self.t(title), style=self.style(1, True))
        self.put(6, 3, "─" * (w - 7), style=self.style(2))
        self.put(h - 3, 3, "─" * (w - 7), style=self.style(2))
        self.put(h - 2, 3, self.status, style=self.style(6 if self.status_error else 2))
        if footer:
            self.put(h - 1, 3, self.t(footer), style=self.style(2))
        return h, w

    def draw_logo(self, y, x, width, height):
        candidates = [
            v
            for v in self.logo["variants"]
            if v["width"] <= width and v["height"] <= height
        ]
        if not candidates:
            return 0
        logo = candidates[-1]
        left = x + max(0, (width - logo["width"]) // 2)
        for dy, (line, colors) in enumerate(zip(logo["text"], logo["colors"])):
            for dx, (char, color) in enumerate(zip(line, colors)):
                if char != " ":
                    self.put(
                        y + dy, left + dx, char, 1, self.ink.get(color, self.style(1))
                    )
        return logo["height"]

    def too_small(self):
        h, w = self.win.getmaxyx()
        if h >= 20 and w >= 58:
            return False
        self.win.erase()
        self.put(0, 0, self.t("ui_resize_terminal"), max(1, w - 1))
        self.win.refresh()
        return True

    def draw_menu(self, title, options, pos, checked=None, detail=""):
        h, w = self.frame(
            title, "ui_checklist_keys" if checked is not None else "ui_menu_keys"
        )
        capacity = max(1, h - 12)
        split = w >= 78
        left_width = max(32, w // 2 - 4) if split else w - 8
        count = len(options) - 1 if checked is not None else len(options)
        start = min(max(0, pos - capacity + 1), max(0, count - capacity))
        for row, i in enumerate(range(start, min(count, start + capacity)), 8):
            label = self.t(options[i])
            prefix = (
                ("[x] " if i in checked else "[ ] ")
                if checked is not None and i < len(options) - 1
                else "  "
            )
            focus = i == pos
            text = ("› " if focus else "  ") + prefix + label
            self.put(
                row,
                3,
                text.ljust(left_width),
                left_width,
                self.style(3, True) if focus else self.style(),
            )
        if checked is not None:
            label = "  " + self.t("apply_changes") + "  "
            self.put(
                h - 4,
                3,
                label,
                style=self.style(3, True) if pos == len(options) - 1 else self.style(2),
            )
        elif len(options) > capacity:
            self.put(h - 4, 3, f"{pos+1} / {len(options)}", style=self.style(2))
        if split:
            x = left_width + 7
            available = w - x - 4
            if title == "main_menu":
                used = self.draw_logo(8, x, available, max(0, h - 12))
                y = 8 + used + 1
            else:
                self.put(8, x, self.t(options[pos]), available, self.style(1, True))
                y = 10
            description = detail or self.t(options[pos] + ".description")
            if hasattr(description, "fit"):
                description = description.fit(available)
            if description == options[pos] + ".description":
                description = ""
            for line in wrapped(description, available)[: max(0, h - 4 - y)]:
                self.put(y, x, line, available, self.style(2))
                y += 1
        self.win.refresh()

    def menu(self, title, options, selected=None, detail=""):
        if not options:
            return None
        rows = list(options) + (["apply_changes"] if selected is not None else [])
        checked = set(selected or []) if selected is not None else None
        pos = min(self.positions.get(title, 0), len(rows) - 1)
        while True:
            if self.too_small():
                if self.win.getch() in (27, ord("q")):
                    return None
                continue
            self.draw_menu(
                title,
                rows,
                pos,
                checked,
                detail.get(rows[pos], "") if isinstance(detail, dict) else detail,
            )
            key = self.win.getch()
            if key in (27, ord("q"), curses.KEY_LEFT):
                return None
            if key in (curses.KEY_UP, ord("k")):
                pos = (pos - 1) % len(rows)
            elif key == 9 and checked is not None:
                pos = 0 if pos == len(rows) - 1 else len(rows) - 1
            elif key in (curses.KEY_DOWN, ord("j"), 9):
                pos = (pos + 1) % len(rows)
            elif key == curses.KEY_HOME:
                pos = 0
            elif key == curses.KEY_END:
                pos = len(rows) - 1
            elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                pos = max(
                    0,
                    min(
                        len(rows) - 1,
                        pos
                        + (1 if key == curses.KEY_NPAGE else -1)
                        * max(1, self.win.getmaxyx()[0] - 11),
                    ),
                )
            elif key == 32 and checked is not None and pos < len(options):
                checked.symmetric_difference_update({pos})
            elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                self.positions[title] = pos
                if checked is None:
                    return pos
                if pos == len(options):
                    return checked
                checked.symmetric_difference_update({pos})
            self.positions[title] = pos

    def document(self, title, text, buttons=("back",), artwork=False):
        offset, button, horizontal = 0, 0, 0
        while True:
            if self.too_small():
                if self.win.getch() in (27, ord("q")):
                    return None
                continue
            h, w = self.frame(title, "ui_document_keys")
            x, width = 3, w - 7
            if artwork and w >= 100:
                art_width = min(58, w // 2 - 5)
                self.draw_logo(8, 3, art_width, h - 13)
                x = art_width + 7
                width = w - x - 4
            content = text.fit(width) if hasattr(text, "fit") else text
            tabular = "│" in content or bool(
                re.search(r"^\+[-+]+\+|^-+\+-+", content, re.M)
            )
            lines = (
                clean(content).expandtabs(4).splitlines()
                if tabular
                else wrapped(content, width)
            )
            horizontal = min(
                horizontal, max(0, max(map(len, lines), default=0) - width)
            )
            capacity = max(1, h - 13)
            offset = min(offset, max(0, len(lines) - capacity))
            for y, line in enumerate(lines[offset : offset + capacity], 8):
                self.put(
                    y,
                    x,
                    line[horizontal:],
                    width,
                    self.resource_style(line),
                )
            if len(lines) > capacity:
                self.put(
                    h - 5,
                    3,
                    f"{offset+1}–{min(len(lines),offset+capacity)} / {len(lines)}",
                    style=self.style(2),
                )
            left = 3
            for i, label in enumerate(buttons):
                text_button = "  " + self.t(label) + "  "
                self.put(
                    h - 4,
                    left,
                    text_button,
                    style=self.style(3, True) if i == button else self.style(2),
                )
                left += len(text_button) + 2
            self.win.refresh()
            key = self.win.getch()
            if key in (27, ord("q")):
                return None
            if key == ord("h"):
                horizontal = max(0, horizontal - 8)
            elif key == ord("l"):
                horizontal += 8
            elif key in (9, curses.KEY_RIGHT):
                button = (button + 1) % len(buttons)
            elif key == curses.KEY_LEFT:
                button = (button - 1) % len(buttons)
            elif key in (curses.KEY_DOWN, ord("j")):
                offset = min(max(0, len(lines) - capacity), offset + 1)
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset = min(max(0, len(lines) - capacity), offset + capacity)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - capacity)
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = max(0, len(lines) - capacity)
            elif key in (10, 13, curses.KEY_ENTER):
                return button

    def input(self, label, default="", secret=False):
        value = list(default)
        cursor = len(value)
        while True:
            if self.too_small():
                if self.win.getch() == 27:
                    raise Cancelled
                continue
            h, w = self.frame("ui_form", "ui_input_keys")
            for i, line in enumerate(wrapped(self.t(label), w - 8)[:3], 8):
                self.put(i, 3, line, style=self.style(1, True))
            width = max(1, w - 10)
            start = max(0, cursor - width + 1)
            rendered = "*" * len(value) if secret else "".join(value)
            self.put(
                13,
                3,
                (" " + rendered[start : start + width] + " ").ljust(w - 7),
                w - 7,
                self.style(3),
            )
            self.win.move(min(13, h - 1), min(w - 2, 4 + cursor - start))
            curses.curs_set(1)
            self.win.refresh()
            key = self.win.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                curses.curs_set(0)
                return "".join(value).strip()
            if key == "\x1b":
                curses.curs_set(0)
                raise Cancelled
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b") and cursor:
                del value[cursor - 1]
                cursor -= 1
            elif key == curses.KEY_DC and cursor < len(value):
                del value[cursor]
            elif key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(value)
            elif key == "\x15":
                value = []
                cursor = 0
            elif isinstance(key, str) and key.isprintable() and len(value) < 4096:
                value.insert(cursor, key)
                cursor += 1

    def progress(self, message, elapsed=0):
        if self.too_small():
            return
        h, w = self.frame("ui_working")
        spin = "|/-\\"[int(time.monotonic() * 5) % 4]
        self.put(
            8,
            3,
            f'{spin}  {self.t("ui_working")}  {elapsed:.0f}s',
            style=self.style(5, True),
        )
        for i, line in enumerate(wrapped(message, w - 8)[: max(1, h - 14)], 10):
            self.put(i, 3, line, style=self.style(2))
        self.win.refresh()

    def monitor(self, sample, services=None, processes=None):
        self.win.timeout(750)
        history = []
        previous = None
        health = []
        last_services = 0
        scroll = 0
        try:
            while True:
                data = sample()
                history = (history + [data["cpu"]])[-60:]
                if services and time.monotonic() - last_services >= 10:
                    health = services()
                    last_services = time.monotonic()
                self.frame("dashboard", "dashboard_keys")
                height, width = self.win.getmaxyx()
                lines = dashboard_lines(
                    data,
                    previous,
                    history,
                    health,
                    processes() if processes else [],
                    self.t,
                    width - 8,
                )
                visible = max(1, height - 12)
                scroll = max(0, min(scroll, len(lines) - visible))
                for y, text in enumerate(lines[scroll : scroll + visible], 8):
                    if " ║ " in text:
                        left, right = text.split(" ║ ", 1)
                        self.put(y, 3, left, style=self.resource_style(left))
                        self.put(y, 3 + len(left), " ║ " + right, style=self.style())
                    else:
                        self.put(y, 3, text, style=self.resource_style(text))
                self.put(
                    height - 4, 3, self.t("dashboard_refresh"), style=self.style(2)
                )
                self.win.refresh()
                key = self.win.getch()
                if key in (27, ord("q"), 10, 13, curses.KEY_ENTER):
                    return
                if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                    scroll += visible if key == curses.KEY_NPAGE else 1
                elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                    scroll -= visible if key == curses.KEY_PPAGE else 1
                previous = data
        finally:
            self.win.timeout(-1)


def dashboard_lines(data, previous, history, services, processes, translate, width=90):
    from .presentation import size

    total_width = width
    wide = width >= 110
    width = width // 2 - 2 if wide else width

    def gauge(label, percent, detail=""):
        columns = max(6, min(28, width - 22 - len(detail)))
        fill = round(columns * max(0, min(100, percent)) / 100)
        return f"{label:<8} [{'█'*fill}{'░'*(columns-fill)}] {percent:5.1f}%  {detail}"

    uptime = int(data.get("uptime", 0))
    lines = [
        f"{data['host']}  ·  {data['time'][:19]} UTC",
        translate(
            "dashboard_uptime",
            days=uptime // 86400,
            hours=uptime // 3600 % 24,
            minutes=uptime // 60 % 60,
            cores=data.get("cores", 1),
            processes=data.get("processes", 0),
        ),
        "",
        gauge("CPU", data["cpu"]),
        gauge(
            "RAM",
            100 * data["ram_used"] / max(1, data["ram_total"]),
            size(data["ram_used"]) + " / " + size(data["ram_total"]),
        ),
        gauge(
            translate("dashboard_disk"),
            100 * data["disk_used"] / max(1, data["disk_total"]),
            size(data["disk_used"]) + " / " + size(data["disk_total"]),
        ),
    ]
    swap = data.get("swap_total", 0)
    lines.append(
        gauge("Swap", 100 * data.get("swap_used", 0) / max(1, swap), size(swap))
        if swap
        else translate("dashboard_no_swap")
    )
    if data.get("inodes_total"):
        lines.append(
            gauge("Inodes", 100 * (1 - data["inodes_free"] / data["inodes_total"]))
        )
    lines += [
        translate("dashboard_load")
        + ": "
        + " / ".join(f"{n:.2f}" for n in data["load"]),
        "",
        translate("dashboard_cpu_history") + " · %",
    ]
    columns = max(12, min(60, width - 10))
    points = history[-columns:]
    for level in (100, 80, 60, 40, 20):
        cells = "".join(
            " ▁▂▃▄▅▆▇█"[min(8, max(0, math.ceil((value - (level - 20)) * 8 / 20)))]
            for value in points
        )
        lines.append(f"{level:3} │" + cells.ljust(columns) + "│")
    lines.append("  0 └" + "─" * columns + "┘")
    lines += ["", translate("dashboard_network")]
    elapsed = data.get("monotonic", 0) - (previous or {}).get("monotonic", 0)

    def priority(name):
        return (
            (
                3
                if name.startswith("veth")
                else (
                    2
                    if name == "lo"
                    else 1 if name.startswith(("docker", "br-", "mosstudents")) else 0
                )
            ),
            name,
        )

    interfaces = sorted(data["network"], key=priority)
    for iface in interfaces[:3]:
        counters = data["network"][iface]
        old = (previous or {}).get("network", {}).get(iface)
        rates = "—"
        if old and elapsed > 0:
            rates = f"↓ {max(0,counters['received']-old['received'])/elapsed/1024:.1f} KiB/s  ↑ {max(0,counters['sent']-old['sent'])/elapsed/1024:.1f} KiB/s"
        lines.append(
            f"{iface:<12} {rates}  RX {size(counters['received'])} / TX {size(counters['sent'])}"
        )
    if len(interfaces) > 3:
        lines.append(translate("dashboard_more_interfaces", count=len(interfaces) - 3))
    right = [translate("dashboard_services")] + [
        f"{s['name']:<26} {translate('dashboard_state_'+s['state'])} ({s['detail']})"
        for s in services
    ]
    if not services:
        right.append("—")
    right += ["", translate("dashboard_processes")] + [
        f"{p['pid']:>7}  {p['name']:<22} {size(p['rss'])}" for p in processes
    ]
    if wide:
        left = [part for line in lines for part in wrapped(line, width)]
        right = [
            part for line in right for part in wrapped(line, total_width - width - 3)
        ]
        return [
            (left[i] if i < len(left) else "").ljust(width)
            + " ║ "
            + (right[i] if i < len(right) else "")
            for i in range(max(len(left), len(right)))
        ]
    return lines + [""] + right
