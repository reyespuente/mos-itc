"""Small terminal-friendly tables shared by inventory screens."""

import shutil
import textwrap
from .terminal import clean


def size(value):
    number = max(0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024


def rows(values, limits=None):
    values = [[clean(str(c)).replace("\n", " ") for c in row] for row in values]
    if not values:
        return []
    widths = [max(len(r[i]) for r in values) for i in range(len(values[0]))]
    if limits:
        widths = [min(a, b) for a, b in zip(widths, limits)]
    return [
        "  ".join(
            (c if len(c) <= w else c[: max(0, w - 1)] + "…").ljust(w)
            for c, w in zip(row, widths)
        ).rstrip()
        for row in values
    ]


def _render(headers, values, width=None):
    width = max(24, width or min(120, shutil.get_terminal_size().columns - 8))
    values = [[clean(str(c)).replace("\n", " ") for c in r] for r in values]
    values.insert(0, [clean(str(h)) for h in headers])
    count = len(headers)
    widths = [max(len(r[i]) for r in values) for i in range(count)]
    while sum(widths) + 3 * (count - 1) > width and max(widths) > 3:
        i = widths.index(max(widths))
        widths[i] -= 1
    result = []
    for index, row in enumerate(values):
        cells = [
            textwrap.wrap(c, w, replace_whitespace=False) or [""]
            for c, w in zip(row, widths)
        ]
        for line in range(max(map(len, cells))):
            result.append(
                " │ ".join(
                    (c[line] if line < len(c) else "").ljust(w)
                    for c, w in zip(cells, widths)
                ).rstrip()
            )
        if index == 0:
            result.append("─┼─".join("─" * w for w in widths))
    return "\n".join(result)


class Table(str):
    """Retain cells so a report can reflow when its terminal pane is resized."""

    def __new__(cls, headers, values, width=None):
        headers = list(headers)
        values = [list(row) for row in values]
        result = super().__new__(cls, _render(headers, values, width))
        result.headers = headers
        result.values = values
        result.width = width
        result.cached_width = None
        result.cached_text = None
        return result

    def fit(self, width):
        width = min(self.width, width) if self.width else width
        if width != self.cached_width:
            self.cached_text = _render(self.headers, self.values, width)
            self.cached_width = width
        return self.cached_text


def table(headers, values, width=None):
    return Table(headers, values, width)
