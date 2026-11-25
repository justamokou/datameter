"""Dependency-free terminal chart rasterizers (Braille lines and eighth-block bars)."""
from __future__ import annotations

BAR_EIGHTHS = " ▁▂▃▄▅▆▇█"

BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def bar_cells(value: float | None, maximum: float, height: int) -> dict[int, str]:
    """Map a single bar value onto terminal rows (0=top..height-1=bottom) at 8x resolution."""
    if value is None or value <= 0 or height <= 0 or maximum <= 0:
        return {}
    steps = max(1, min(height * 8, round(value / maximum * height * 8)))
    full, rem = divmod(steps, 8)
    cells: dict[int, str] = {}
    for i in range(full):
        cells[height - 1 - i] = "█"
    if rem and full < height:
        cells[height - 1 - full] = BAR_EIGHTHS[rem]
    return cells


def _plot_braille_dot(masks: dict[tuple[int, int], int], sx: int, sy: int,
                      sub_w: int, sub_h: int) -> None:
    if 0 <= sx < sub_w and 0 <= sy < sub_h:
        row, py = divmod(sy, 4)
        col, px = divmod(sx, 2)
        masks[(row, col)] = masks.get((row, col), 0) | BRAILLE_BITS[py][px]


def braille_line_grid(download: list, upload: list, cell_width: int,
                      height: int, maximum: float) -> dict[tuple[int, int], tuple[str, int]]:
    """Rasterize two series onto a 2x4 Unicode Braille grid without bridging None gaps."""
    if height <= 0 or cell_width <= 0 or maximum <= 0:
        return {}
    count = max(len(download), len(upload))
    sub_w = count * cell_width * 2
    sub_h = height * 4
    series_masks: list[dict[tuple[int, int], int]] = [{}, {}]

    for s_idx, values in enumerate((download, upload)):
        masks = series_masks[s_idx]
        previous: tuple[int, int] | None = None
        for index, value in enumerate(values):
            if value is None:
                previous = None
                continue
            sx = index * cell_width * 2 + cell_width - (cell_width % 2)
            sy = sub_h - 1 - round(value / maximum * (sub_h - 1))
            sy = max(0, min(sub_h - 1, sy))
            has_next = index + 1 < len(values) and values[index + 1] is not None
            if previous is not None:
                px, py = previous
                dx, dy = abs(sx - px), -abs(sy - py)
                step_x = 1 if px < sx else -1
                step_y = 1 if py < sy else -1
                error = dx + dy
                while True:
                    _plot_braille_dot(masks, px, py, sub_w, sub_h)
                    # Thicken stroke vertically by 1 sub-pixel so thin terminal fonts stay legible.
                    _plot_braille_dot(masks, px, py + 1 if py + 1 < sub_h else py - 1, sub_w, sub_h)
                    if (px, py) == (sx, sy):
                        break
                    twice = 2 * error
                    if twice >= dy:
                        error += dy
                        px += step_x
                    if twice <= dx:
                        error += dx
                        py += step_y
            else:
                _plot_braille_dot(masks, sx, sy, sub_w, sub_h)
                _plot_braille_dot(masks, sx, sy + 1 if sy + 1 < sub_h else sy - 1, sub_w, sub_h)
                if not has_next:
                    nx = sx - 1 if (sx % 2) else sx + 1
                    ny = sy + 1 if sy + 1 < sub_h else sy - 1
                    _plot_braille_dot(masks, nx, sy, sub_w, sub_h)
                    _plot_braille_dot(masks, nx, ny, sub_w, sub_h)
            previous = (sx, sy)

    grid: dict[tuple[int, int], tuple[str, int]] = {}
    all_cells = set(series_masks[0]) | set(series_masks[1])
    for pos in all_cells:
        m1 = series_masks[0].get(pos, 0)
        m2 = series_masks[1].get(pos, 0)
        series = 3 if (m1 and m2) else (1 if m1 else 2)
        grid[pos] = (chr(0x2800 | m1 | m2), series)
    return grid


def line_grid(download: list, upload: list, cell_width: int,
              height: int, maximum: float) -> dict:
    """Legacy character-cell line-chart rasterizer."""
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
