from PIL import Image, ImageDraw
import math

def draw_weather_icon(draw, cx, cy, code):
    # 0: Clear
    # 1, 2: Partly Cloudy
    # 3: Overcast
    # 45, 48: Fog
    # 51-67, 80-82: Rain
    # 71-77, 85-86: Snow
    # 95-99: Thunderstorm
    
    def draw_sun(x, y, radius=40, rays=True):
        # outline
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="#fbbf24", width=3)
        draw.ellipse((x - radius+5, y - radius+5, x + radius-5, y + radius-5), fill="#fbbf24")
        if rays:
            for i in range(8):
                angle = i * (math.pi / 4)
                r1 = radius + 8
                r2 = radius + 20
                lx1 = x + math.cos(angle) * r1
                ly1 = y + math.sin(angle) * r1
                lx2 = x + math.cos(angle) * r2
                ly2 = y + math.sin(angle) * r2
                draw.line((lx1, ly1, lx2, ly2), fill="#fbbf24", width=4)

    def draw_cloud(x, y, fill="white", outline="white"):
        # center bottom, left, right, top
        circles = [
            (x - 35, y - 10, x + 35, y + 30),
            (x - 60, y + 5, x - 15, y + 35),
            (x + 15, y + 5, x + 60, y + 35),
            (x - 25, y - 25, x + 25, y + 15),
        ]
        for bb in circles:
            draw.ellipse(bb, fill=fill, outline=outline, width=3)
        # Erase inner overlapping outlines
        for (bx1, by1, bx2, by2) in circles:
            draw.ellipse((bx1+3, by1+3, bx2-3, by2-3), fill=fill)

    if code == 0:
        draw_sun(cx, cy)
    elif code in (1, 2):
        draw_sun(cx + 20, cy - 10, radius=30, rays=True)
        draw_cloud(cx - 10, cy + 10)
    elif code == 3:
        draw_cloud(cx, cy)
    elif code in (45, 48): # Fog
        draw_cloud(cx, cy - 20)
        for i in range(3):
            draw.line((cx - 40, cy + 25 + i*10, cx + 40, cy + 25 + i*10), fill="white", width=4)
    elif code in range(51, 68) or code in range(80, 83): # Rain
        draw_cloud(cx, cy - 15)
        for i in range(-2, 3):
            draw.line((cx + i*15, cy + 25, cx + i*15 - 10, cy + 45), fill="#60a5fa", width=3)
    elif code in range(71, 78) or code in range(85, 87): # Snow
        draw_cloud(cx, cy - 15)
        for i in range(-2, 3):
            draw.ellipse((cx + i*15 - 3, cy + 30 - 3 + (i%2)*10, cx + i*15 + 3, cy + 30 + 3 + (i%2)*10), fill="white")
    elif code >= 95: # Thunderstorm
        draw_cloud(cx, cy - 15, fill="#a1a1aa")
        # Lightning bolt polygon
        draw.polygon([
            (cx + 5, cy + 15),
            (cx - 15, cy + 35),
            (cx, cy + 35),
            (cx - 10, cy + 60),
            (cx + 20, cy + 30),
            (cx + 5, cy + 30),
        ], fill="#fbbf24")
    else:
        # Default
        draw_sun(cx, cy)


def main():
    img = Image.new("RGB", (800, 400), "black")
    draw = ImageDraw.Draw(img)
    codes = [0, 1, 3, 45, 61, 71, 95]
    for i, c in enumerate(codes):
        x = 100 + i * 100
        draw_weather_icon(draw, x, 200, c)
    img.save("weather_icons_test.png")

if __name__ == "__main__":
    main()
