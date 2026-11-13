"""A small, dependency-free line-chart rasterizer for terminal cells."""
from __future__ import annotations


def line_grid(download: list, upload: list, cell_width: int,
              height: int, maximum: float) -> dict:
    grid = {}

    def put(x, y, char, series):
        if (y, x) in grid and grid[y, x][1] != series:
            grid[y, x] = ("×", 3)
        elif (y, x) in grid and grid[y, x][0] in ("●", "◆") and char in ("─", "┄"):
            return
        else:
            grid[y, x] = (char, series)

    for series, values in enumerate((download, upload), 1):
        previous = None
        for index, value in enumerate(values):
            if value is None:
                previous = None
                continue
            x = index * cell_width + cell_width // 2
            y = height - 1 - round(value / maximum * (height - 1))
            y = max(0, min(height - 1, y))
            if previous:
                px, py = previous
                dx, dy = abs(x - px), -abs(y - py)
                sx, sy = (1 if px < x else -1), (1 if py < y else -1)
                error = dx + dy
                while (px, py) != (x, y):
                    put(px, py, "─" if series == 1 else "┄", series)
                    twice = 2 * error
                    if twice >= dy:
                        error += dy
                        px += sx
                    if twice <= dx:
                        error += dx
                        py += sy
            put(x, y, "●" if series == 1 else "◆", series)
            previous = (x, y)
    return grid
