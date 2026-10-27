"""Consistent native vector symbols and tonal action palettes; no image assets."""

PALETTES = {
    "chat": {"background": "#e8f0ff", "hover": "#dbe7fb", "foreground": "#174b85", "border": "#b5cbe7"},
    "details": {"background": "#f1eafb", "hover": "#e7dbf5", "foreground": "#593587", "border": "#cfbce7"},
    "file": {"background": "#e3f3ef", "hover": "#d3eae3", "foreground": "#145c53", "border": "#acd3c8"},
}


def contrast_ratio(foreground, background):
    def luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    first, second = sorted((luminance(foreground), luminance(background)))
    return (second + 0.05) / (first + 0.05)


def draw_icon(canvas, kind, foreground, scale=1.0):
    """Draw one visual family in a 28×28 logical pixel box using Tk vectors."""
    def line(points, **kwargs):
        return canvas.create_line(*[p * scale for p in points], fill=foreground, width=1.6 * scale,
                                  capstyle="round", joinstyle="round", **kwargs)
    if kind == "CHAT":
        line((4, 4, 24, 4, 24, 19, 14, 19, 8, 24, 8, 19, 4, 19, 4, 4))
        line((8, 9, 20, 9))
        line((8, 13, 17, 13))
    elif kind == "URL":
        canvas.create_arc(3 * scale, 11 * scale, 16 * scale, 24 * scale, start=30, extent=290,
                          style="arc", outline=foreground, width=1.8 * scale)
        canvas.create_arc(12 * scale, 3 * scale, 25 * scale, 16 * scale, start=210, extent=290,
                          style="arc", outline=foreground, width=1.8 * scale)
        line((10, 18, 18, 10))
    else:
        line((6, 3, 17, 3, 23, 9, 23, 25, 6, 25, 6, 3))
        line((17, 3, 17, 9, 23, 9))
        if kind in {"DETAILS", "FILE"}:
            for y, end in ((13, 19), (17, 19), (21, 16)):
                line((10, y, end, y))
        else:
            glyph = {"WORD": "W", "HTML": "<>", "IMAGE": "IMG", "JSON": "{}", "CSV": "CSV"}.get(kind, kind)
            canvas.create_text(14.5 * scale, 17 * scale, text=glyph, fill=foreground,
                               font=("Segoe UI", -round(7 * scale), "bold"))
