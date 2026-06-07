from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from inky_slideshow.slideshow import AppConfig, WeatherSnapshot, render_weather_screen_pillow

def main():
    snapshot = WeatherSnapshot(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        location_name="London",
        temperature_c=13,
        feels_like_c=8,
        weather_code=95,
        wind_mph=11,
        uv_index=2,
        air_quality_index=2,
        sunrise="2026-06-07T04:43",
        sunset="2026-06-07T21:17",
        hourly=[],
    )
    now = datetime(2026, 6, 7, 9, 23, tzinfo=timezone.utc)
    config = AppConfig()
    resolution = (800, 480)
    
    img = render_weather_screen_pillow(resolution, config, snapshot, now=now)
    img.save("weather_layout_test.png")
    print("Saved weather_layout_test.png")

if __name__ == "__main__":
    main()
