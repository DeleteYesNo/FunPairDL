"""
Generate simple blue download arrow icons for the Chrome extension.
Creates icon16.png, icon48.png, and icon128.png in extension/icons/.

Requires Pillow: pip install Pillow
"""

from PIL import Image, ImageDraw
import os


def generate_icon(size, output_path):
    """Generate a blue circle with a white downward arrow icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw blue circle background
    padding = max(1, size // 16)
    draw.ellipse(
        [padding, padding, size - padding - 1, size - padding - 1],
        fill=(66, 133, 244, 255),
    )

    # Calculate arrow dimensions relative to icon size
    cx = size / 2  # center x
    cy = size / 2  # center y

    # Arrow shaft
    shaft_half_width = size * 0.08
    shaft_top = size * 0.22
    shaft_bottom = size * 0.58

    # Arrow head
    head_half_width = size * 0.25
    head_top = size * 0.50
    head_bottom = size * 0.78

    # Draw the arrow shaft (rectangle)
    draw.rectangle(
        [cx - shaft_half_width, shaft_top, cx + shaft_half_width, shaft_bottom],
        fill=(255, 255, 255, 255),
    )

    # Draw the arrow head (triangle pointing down)
    draw.polygon(
        [
            (cx - head_half_width, head_top),
            (cx + head_half_width, head_top),
            (cx, head_bottom),
        ],
        fill=(255, 255, 255, 255),
    )

    img.save(output_path, "PNG")
    print(f"Created {output_path} ({size}x{size})")


def main():
    icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension", "icons")
    os.makedirs(icons_dir, exist_ok=True)

    for size in (16, 48, 128):
        output_path = os.path.join(icons_dir, f"icon{size}.png")
        generate_icon(size, output_path)

    print("Done.")


if __name__ == "__main__":
    main()
