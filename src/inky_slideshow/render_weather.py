from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from .slideshow import AppConfig, WeatherSnapshot

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    from backports.zoneinfo import ZoneInfo

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
LONDON_TZ = "Europe/London"
KOLKATA_TZ = "Asia/Kolkata"
INKY_BLACK = "#111111"
INKY_BLUE = "#2563eb"
INKY_YELLOW = "#facc15"
INKY_RED = "#dc2626"
INKY_GREEN = "#16a34a"
INKY_ORANGE = "#ea580c"
INKY_MUTED_BLUE = "#a5b4d6"
INKY_MUTED_YELLOW = "#ded49a"
INKY_MUTED_RED = "#d8aaa6"
INKY_MUTED_GREEN = "#a7c4ad"
INKY_MUTED_ORANGE = "#dec0a2"
INKY_DISPLAY_PALETTE = (
    (255, 255, 255),
    (0, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 0, 0),
    (255, 255, 0),
    (255, 128, 0),
)


def _parse_open_meteo_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def render_weather_screen(
    resolution: tuple[int, int],
    config: AppConfig,
    snapshot: WeatherSnapshot | None,
    now: datetime | None = None,
) -> Image.Image:
    return dither_for_inky(render_weather_screen_pillow(resolution, config, snapshot, now))


def render_weather_screen_pillow(
    resolution: tuple[int, int],
    config: AppConfig,
    snapshot: WeatherSnapshot | None,
    now: datetime | None = None,
) -> Image.Image:
    now = now or datetime.now(ZoneInfo(LONDON_TZ))
    image = Image.new("RGB", resolution, "white")
    draw = ImageDraw.Draw(image)
    width, height = resolution
    is_vertical = height > width
    margin = 24 if not is_vertical else 22
    london_now = now.astimezone(ZoneInfo(LONDON_TZ))
    kolkata_now = now.astimezone(ZoneInfo(KOLKATA_TZ))

    weather_code = snapshot.weather_code if snapshot else None
    weather_label = _weather_label(weather_code)
    temp_text = _format_temp(snapshot.temperature_c if snapshot else None)
    feels_text = f"Feels like {_format_temp(snapshot.feels_like_c if snapshot else None)}"

    if not is_vertical:
        gap = 14
        current_box = (margin, margin, 378, 274)
        clock_box = (392, margin, width - margin, 274)
        bottom_y = 288
        footer_h = 34
        card_h = height - bottom_y - footer_h - margin - gap
        card_w = (width - (margin * 2) - (gap * 3)) // 4

        _draw_panel(draw, current_box, INKY_YELLOW, density="medium")
        _draw_panel(draw, clock_box, INKY_BLUE, density="light")
        _draw_current_card(draw, current_box, config.location_name, weather_code, weather_label, temp_text, feels_text)
        _draw_clock_card(draw, clock_box, london_now, kolkata_now)

        bottom_cards = [
            (
                "TODAY",
                _format_temp_range(snapshot.today_low_c if snapshot else None, snapshot.today_high_c if snapshot else None),
                f"{_time_label(snapshot.sunrise) if snapshot else '--:--'} / {_time_label(snapshot.sunset) if snapshot else '--:--'}",
                "\uf051",
                INKY_ORANGE,
            ),
            (
                "TOMORROW",
                _short_weather_label(snapshot.tomorrow_weather_code if snapshot else None),
                _format_temp_range(snapshot.tomorrow_low_c if snapshot else None, snapshot.tomorrow_high_c if snapshot else None),
                _weather_icon_char(snapshot.tomorrow_weather_code if snapshot else None),
                INKY_BLUE,
            ),
            (
                "COMFORT",
                f"{_format_number(snapshot.humidity_percent if snapshot else None)}%",
                f"UV {_uv_label(snapshot.uv_index if snapshot else None)}",
                "\uf07a",
                INKY_GREEN,
            ),
            (
                "AIR / WIND",
                _aqi_label(snapshot.air_quality_index if snapshot else None),
                f"{_format_number(snapshot.wind_mph if snapshot else None)} mph",
                "\uf050",
                INKY_RED,
            ),
        ]
        for index, card in enumerate(bottom_cards):
            x = margin + index * (card_w + gap)
            _draw_info_card(draw, (x, bottom_y, x + card_w, bottom_y + card_h), *card)

        footer_box = (margin, height - margin - footer_h, width - margin, height - margin)
        _draw_footer(draw, footer_box, _admin_url_label(config), london_now)
    else:
        gap = 12
        current_box = (margin, margin, width - margin, 282)
        clock_box = (margin, 294, width - margin, 424)
        _draw_panel(draw, current_box, INKY_YELLOW, density="medium")
        _draw_panel(draw, clock_box, INKY_BLUE, density="light")
        _draw_current_card(draw, current_box, config.location_name, weather_code, weather_label, temp_text, feels_text, compact=True)
        _draw_clock_card(draw, clock_box, london_now, kolkata_now, compact=True)

        card_y = 436
        card_h = 62
        cards = [
            ("TODAY", _format_temp_range(snapshot.today_low_c if snapshot else None, snapshot.today_high_c if snapshot else None), "LOW / HIGH", "\uf053", INKY_ORANGE),
            ("TOMORROW", _short_weather_label(snapshot.tomorrow_weather_code if snapshot else None), _format_temp_range(snapshot.tomorrow_low_c if snapshot else None, snapshot.tomorrow_high_c if snapshot else None), _weather_icon_char(snapshot.tomorrow_weather_code if snapshot else None), INKY_BLUE),
            ("COMFORT", f"{_format_number(snapshot.humidity_percent if snapshot else None)}%", f"UV {_uv_label(snapshot.uv_index if snapshot else None)}", "\uf07a", INKY_GREEN),
            ("AIR / WIND", _aqi_label(snapshot.air_quality_index if snapshot else None), f"{_format_number(snapshot.wind_mph if snapshot else None)} mph", "\uf050", INKY_RED),
        ]
        for index, card in enumerate(cards):
            _draw_info_card(draw, (margin, card_y + index * (card_h + gap), width - margin, card_y + index * (card_h + gap) + card_h), *card, compact=True)
        _draw_footer(draw, (margin, height - margin - 30, width - margin, height - margin), _admin_url_label(config), london_now)

    if snapshot is None:
        draw.text((width - margin - 8, height - margin - 8), "WEATHER UNAVAILABLE", font=_font(16, "Bold"), fill=INKY_RED, anchor="rb")

    return image


def dither_for_inky(image: Image.Image) -> Image.Image:
    palette = Image.new("P", (1, 1))
    palette_values: list[int] = []
    for red, green, blue in INKY_DISPLAY_PALETTE:
        palette_values.extend((red, green, blue))
    palette_values.extend([0] * (768 - len(palette_values)))
    palette.putpalette(palette_values)
    return image.convert("RGB").quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = FONT_DIR / f"Inter-{weight}.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def _draw_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str, density: str = "light") -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=12, outline=INKY_BLACK, width=3, fill="white")
    inset = 7
    spacing = 4 if density == "light" else 3
    radius = 2 if density == "light" else 2
    _draw_halftone(draw, (x1 + inset, y1 + inset, x2 - inset, y2 - inset), spacing=spacing, radius=radius, fill=_muted_accent(accent))


def _draw_halftone(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    spacing: int = 6,
    radius: int = 1,
    fill: str = INKY_BLACK,
    offset: int = 0,
) -> None:
    x1, y1, x2, y2 = box
    for row, y in enumerate(range(y1, y2, spacing)):
        shift = spacing // 2 if (row + offset) % 2 else 0
        for x in range(x1 + shift, x2, spacing):
            draw.ellipse((x, y, x + radius, y + radius), fill=fill)


def _muted_accent(accent: str) -> str:
    return {
        INKY_BLUE: INKY_MUTED_BLUE,
        INKY_YELLOW: INKY_MUTED_YELLOW,
        INKY_RED: INKY_MUTED_RED,
        INKY_GREEN: INKY_MUTED_GREEN,
        INKY_ORANGE: INKY_MUTED_ORANGE,
    }.get(accent, accent)


def _draw_current_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    location: str,
    weather_code: int | None,
    weather_label: str,
    temp_text: str,
    feels_text: str,
    compact: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    title_font = _font(18 if not compact else 16, "Bold")
    temp_font = _font(92 if not compact else 78, "Black")
    unit_font = _font(54 if not compact else 46, "Black")
    body_font = _font(18 if not compact else 16, "Medium")
    label_font = _font(16 if not compact else 14, "Bold")
    icon_size = 118 if not compact else 94

    draw.text((x1 + 22, y1 + 22), location, font=title_font, fill=INKY_BLACK, anchor="lt")
    _draw_weather_icon(draw, x1 + 94, y1 + (134 if not compact else 118), weather_code, icon_size)
    _draw_temperature(draw, (x1 + 178, y1 + (138 if not compact else 126)), temp_text, temp_font, unit_font)
    _draw_fitted_text(draw, feels_text, (x1 + 180, y1 + (156 if not compact else 143), x2 - 18, y1 + (184 if not compact else 168)), body_font, INKY_BLACK, "lm")
    _draw_fitted_text(draw, weather_label.upper(), (x1 + 24, y2 - 42, x2 - 22, y2 - 17), label_font, INKY_BLACK, "lm")


def _draw_clock_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    london_now: datetime,
    kolkata_now: datetime,
    compact: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    date_font = _font(42 if not compact else 30, "Black")
    day_font = _font(22 if not compact else 18, "Bold")
    label_font = _font(16 if not compact else 14, "Bold")
    time_font = _font(42 if not compact else 32, "Black")
    small_font = _font(15 if not compact else 13, "Medium")

    draw.text((x1 + 24, y1 + 24), london_now.strftime("%A").upper(), font=day_font, fill=INKY_BLACK, anchor="lt")
    draw.text((x1 + 24, y1 + (88 if not compact else 70)), london_now.strftime("%-d %B"), font=date_font, fill=INKY_BLACK, anchor="lm")
    draw.text((x1 + 24, y1 + (130 if not compact else 100)), london_now.strftime("%Y"), font=small_font, fill=INKY_BLACK, anchor="lt")

    row_y = y1 + (164 if not compact else 58)
    rows = [
        ("LONDON", london_now.strftime("%H:%M"), INKY_BLUE),
        ("KOLKATA", kolkata_now.strftime("%H:%M"), INKY_GREEN),
    ]
    for index, (label, value, accent) in enumerate(rows):
        if compact:
            x = x1 + 210 + index * 132
            _draw_time_cell(draw, (x, y1 + 44, x + 116, y2 - 18), label, value, accent, label_font, _font(28, "Black"))
        else:
            _draw_time_cell(draw, (x1 + 24 + index * 172, row_y, x1 + 176 + index * 172, y2 - 22), label, value, accent, label_font, time_font)


def _draw_time_cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    accent: str,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    value_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=8, outline=INKY_BLACK, width=2, fill="white")
    _draw_halftone(draw, (x1 + 6, y1 + 6, x2 - 6, y2 - 6), spacing=4, radius=2, fill=_muted_accent(accent), offset=1)
    draw.text((x1 + 12, y1 + 13), label, font=label_font, fill=INKY_BLACK, anchor="lt")
    _draw_fitted_text(draw, value, (x1 + 12, y1 + 30, x2 - 10, y2 - 8), value_font, INKY_BLACK, "lb")


def _draw_info_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    detail: str,
    icon: str,
    accent: str,
    compact: bool = False,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=10, outline=INKY_BLACK, width=2, fill="white")
    _draw_halftone(draw, (x1 + 6, y1 + 6, x2 - 6, y1 + 27), spacing=4, radius=2, fill=_muted_accent(accent))
    label_font = _font(13 if not compact else 12, "Bold")
    value_font = _font(25 if not compact else 20, "Black")
    detail_font = _font(14 if not compact else 12, "Medium")
    icon_font = _weather_font(24 if not compact else 20)

    draw.text((x1 + 12, y1 + 12), label, font=label_font, fill=INKY_BLACK, anchor="lt")
    draw.text((x2 - 12, y1 + 18), icon, font=icon_font, fill=INKY_BLACK, anchor="mm")
    _draw_fitted_text(draw, value, (x1 + 12, y1 + 35, x2 - 12, y1 + 68), value_font, INKY_BLACK, "lb")
    _draw_fitted_text(draw, detail, (x1 + 12, y2 - 25, x2 - 12, y2 - 8), detail_font, INKY_BLACK, "lb")


def _draw_footer(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], admin_url: str, now: datetime) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=8, outline=INKY_BLACK, width=2, fill="white")
    _draw_halftone(draw, (x1 + 6, y1 + 6, x2 - 6, y2 - 6), spacing=4, radius=2, fill=INKY_MUTED_BLUE)
    font = _font(14, "Bold")
    draw.text((x1 + 14, y1 + (y2 - y1) // 2), f"ADMIN {admin_url}", font=font, fill=INKY_BLACK, anchor="lm")
    draw.text((x2 - 14, y1 + (y2 - y1) // 2), f"UPDATED {now.strftime('%H:%M')}", font=font, fill=INKY_BLACK, anchor="rm")


def _draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    anchor: str,
) -> None:
    x1, y1, x2, y2 = box
    candidate = font
    while isinstance(candidate, ImageFont.FreeTypeFont):
        bbox = draw.textbbox((0, 0), text, font=candidate)
        if bbox[2] - bbox[0] <= x2 - x1 and bbox[3] - bbox[1] <= y2 - y1:
            break
        if candidate.size <= 10:
            break
        candidate = _font(candidate.size - 1, _font_weight(candidate))
    anchor_x = x1 if "l" in anchor else x2 if "r" in anchor else (x1 + x2) // 2
    anchor_y = y1 if "t" in anchor else y2 if "b" in anchor else (y1 + y2) // 2
    draw.text((anchor_x, anchor_y), text, font=candidate, fill=fill, anchor=anchor)


def _font_weight(font: ImageFont.FreeTypeFont) -> str:
    name = font.getname()[1]
    if "Black" in name:
        return "Black"
    if "Bold" in name:
        return "Bold"
    if "Medium" in name:
        return "Medium"
    return "Regular"


def _weather_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = FONT_DIR / "weathericons.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def _draw_temperature(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: str,
    number_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    unit_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    x, baseline = origin
    if text.endswith("C") and text != "--":
        number = text[:-1]
        draw.text((x, baseline), number, font=number_font, fill=INKY_BLACK, anchor="ls")
        bbox = draw.textbbox((x, baseline), number, font=number_font, anchor="ls")
        draw.text((bbox[2] + 8, baseline - 9), "C", font=unit_font, fill=INKY_RED, anchor="ls")
        return
    draw.text((x, baseline), text, font=number_font, fill=INKY_BLACK, anchor="ls")

def _draw_weather_icon(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    code: int | None,
    icon_size: int = 160,
) -> None:
    char = _weather_icon_char(code)

    font_path = FONT_DIR / "weathericons.ttf"
    if font_path.exists():
        icon_font = ImageFont.truetype(str(font_path), icon_size)
        draw.text((cx + 4, cy + 4), char, font=icon_font, fill=INKY_BLACK, anchor="mm")
        draw.text((cx, cy), char, font=icon_font, fill=_weather_icon_color(code), anchor="mm")
    else:
        draw.ellipse((cx - 40, cy - 40, cx + 40, cy + 40), outline=_weather_icon_color(code), width=8)


def _weather_icon_color(code: int | None) -> str:
    if code is None:
        return INKY_BLUE
    if code == 0 or code in (1, 2):
        return INKY_YELLOW
    if code >= 95:
        return INKY_RED
    if code in range(51, 68) or code in range(80, 83):
        return INKY_BLUE
    if code in range(71, 78) or code in range(85, 87):
        return INKY_BLUE
    return INKY_BLUE


def _weather_icon_char(code: int | None) -> str:
    if code is None:
        return "\uf00d"
    if code == 0:
        return "\uf00d"
    if code in (1, 2):
        return "\uf002"
    if code == 3:
        return "\uf013"
    if code in (45, 48):
        return "\uf014"
    if code in range(51, 56) or code in range(56, 60):
        return "\uf01c"
    if code in range(61, 68):
        return "\uf019"
    if code in range(71, 78) or code in range(85, 87):
        return "\uf01b"
    if code in range(80, 83):
        return "\uf01a"
    if code >= 95:
        return "\uf01e"
    return "\uf00d"


def _weather_label(code: int | None) -> str:
    if code is None:
        return "Weather"
    if code == 0:
        return "Clear"
    if code in (1, 2):
        return "Partly cloudy"
    if code == 3:
        return "Overcast"
    if code in (45, 48):
        return "Fog"
    if code in range(51, 68) or code in range(80, 83):
        return "Rain"
    if code in range(71, 78) or code in range(85, 87):
        return "Snow"
    if code >= 95:
        return "Storm"
    return "Weather"


def _short_weather_label(code: int | None) -> str:
    label = _weather_label(code)
    return "Cloudy" if label == "Partly cloudy" else label


def _format_temp(value: float | None) -> str:
    return "--" if value is None else f"{round(value)}C"


def _format_temp_range(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "--"
    return f"{round(low)}-{round(high)}C"


def _format_number(value: float | None) -> str:
    return "--" if value is None else str(round(value))


def _uv_label(value: float | None) -> str:
    if value is None:
        return "--"
    level = "Low" if value < 3 else "Med" if value < 6 else "High"
    return f"{round(value)} {level}"


def _aqi_label(value: int | None) -> str:
    if value is None:
        return "--"
    if value < 20:
        level = "Good"
    elif value < 40:
        level = "Fair"
    elif value < 60:
        level = "Mod"
    elif value < 80:
        level = "Poor"
    else:
        level = "Bad"
    return f"{value} {level}"


def _time_label(value: str | None) -> str:
    parsed = _parse_open_meteo_time(value) if value else None
    return "--:--" if parsed is None else parsed.strftime("%H:%M")


def _admin_url_label(config: AppConfig) -> str:
    if config.host in {"0.0.0.0", "::", ""}:
        return f"zero-frame.local:{config.port}"
    return f"{config.host}:{config.port}"


def main() -> None:
    from .slideshow import AppConfig, ConfigStore, WeatherClient, oriented_resolution

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--resolution", default="800x480")
    args = parser.parse_args()

    width_text, height_text = args.resolution.lower().split("x", 1)
    resolution = (int(width_text), int(height_text))
    store = ConfigStore(Path(args.config), AppConfig())
    config = store.load()
    snapshot = WeatherClient().fetch_or_cached(config)
    image = render_weather_screen(oriented_resolution(resolution, config.frame_orientation), config, snapshot)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    sys.stdout.buffer.write(buffer.getvalue())


if __name__ == "__main__":
    main()
