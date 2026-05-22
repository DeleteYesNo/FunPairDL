"""Generate FunPairDL icon as .ico and .png files."""
from PIL import Image, ImageDraw, ImageFont
import math


def create_icon(size: int) -> Image.Image:
    """Create a FunPairDL icon at the given size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = size / 2, size / 2
    r = size * 0.45  # radius for rounded rect background

    # Background: rounded rectangle, dark blue (#1a1a2e → #4a90d9 gradient feel)
    # Use a circle as base shape for simplicity at small sizes
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        fill="#1e3a5f",
        outline="#4a90d9",
        width=max(1, size // 32),
    )

    # Download arrow
    arrow_w = size * 0.28
    arrow_h = size * 0.22
    arrow_cx = cx
    arrow_top = cy - size * 0.18

    # Arrow shaft (vertical bar)
    shaft_w = size * 0.08
    draw.rectangle(
        [
            arrow_cx - shaft_w / 2, arrow_top,
            arrow_cx + shaft_w / 2, arrow_top + arrow_h,
        ],
        fill="#4a90d9",
    )

    # Arrow head (triangle pointing down)
    head_y = arrow_top + arrow_h
    head_h = size * 0.12
    draw.polygon(
        [
            (arrow_cx - arrow_w / 2, head_y),
            (arrow_cx + arrow_w / 2, head_y),
            (arrow_cx, head_y + head_h),
        ],
        fill="#4a90d9",
    )

    # Horizontal line (download tray)
    tray_y = head_y + head_h + size * 0.04
    tray_w = size * 0.36
    line_h = max(2, size // 32)
    draw.rectangle(
        [cx - tray_w / 2, tray_y, cx + tray_w / 2, tray_y + line_h],
        fill="#4a90d9",
    )

    # "FP" text at the bottom
    try:
        font_size = max(8, int(size * 0.16))
        font = ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    text = "FP"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    text_y = tray_y + line_h + size * 0.02
    draw.text(
        (cx - tw / 2, text_y),
        text,
        fill="#8ab4e8",
        font=font,
    )

    return img


def main():
    # Generate multiple sizes for .ico
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [create_icon(s) for s in sizes]

    # Save as .ico (multi-size)
    ico_path = "assets/funpairdl.ico"
    images[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[:-1],
    )
    print(f"Saved: {ico_path}")

    # Save large PNG for reference
    png_path = "assets/funpairdl.png"
    create_icon(256).save(png_path)
    print(f"Saved: {png_path}")


if __name__ == "__main__":
    main()
