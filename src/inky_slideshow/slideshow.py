from glob import glob
import time
import click
from inky.auto import auto
from PIL import Image


@click.command()
@click.argument("path", type=click.Path(exists=True))
def main(path):
    inky_display = auto(ask_user=True)
    inky_display.set_border(inky_display.WHITE)

    images = []

    allowed_extensions = [".png", ".jpg", ".jpeg"]

    for ext in allowed_extensions:
        images.extend(glob(f"{path}/*{ext}"))

    index = 0

    while True:
        current_image = images[index % len(images)]
        image = Image.open(current_image).convert("RGB")
        image = image.resize(inky_display.resolution)
        inky_display.set_image(image)
        inky_display.show()
        index += 1

        time.sleep(60)


if __name__ == "__main__":
    main()
