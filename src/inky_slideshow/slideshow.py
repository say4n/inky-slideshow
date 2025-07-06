import click
from inky.auto import auto
from PIL import Image

def main():
    inky_display = auto()
    inky_display.set_border(inky_display.WHITE)

    # Load an image
    image = Image.open("path/to/your/image.png").convert("RGB")

    # Resize the image to fit the display
    image = image.resize(inky_display.resolution)

    # Display the image
    inky_display.set_image(image)
    inky_display.show()

if __name__ == "__main__":
    main()