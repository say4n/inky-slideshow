from glob import glob
import random
import time
import click
from inky.auto import auto
from loguru import logger
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

    if not images:
        logger.error("No images found in the specified directory.")
        exit(1)
    else:
        logger.info(f"Found {len(images)} images in the directory: {path}")

    index = random.randint(0, len(images) - 1)

    while True:
        current_image = images[index % len(images)]
        logger.info(f"Displaying image: {current_image} (Index: {index})")

        image = Image.open(current_image).convert("RGB")
        image = image.resize(inky_display.resolution)
        inky_display.set_image(image)
        inky_display.show()
        index += 1

        logger.info("Waiting for 60 seconds before displaying the next image...")
        time.sleep(60)


if __name__ == "__main__":
    main()
