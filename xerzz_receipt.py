from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram.types import BufferedInputFile


def font(size, bold=False):
    path = (
        "C:/Windows/Fonts/arialbd.ttf"
        if bold else "C:/Windows/Fonts/arial.ttf"
    )
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def make_receipt(product, amount):
    w, h = 1080, 1350
    img = Image.new("RGB", (w, h), "#061329")
    draw = ImageDraw.Draw(img)

    for y in range(h):
        draw.line(
            (0, y, w, y),
            fill=(5, 15 + y // 100, 38 + y // 70)
        )

    draw.rounded_rectangle(
        (35, 35, 1045, 1315),
        radius=40,
        outline="#168CFF",
        width=5
    )

    def center(y, text, size, color, bold=True):
        f = font(size, bold)
        box = draw.textbbox((0, 0), text, font=f)
        x = (w - (box[2] - box[0])) // 2
        draw.text((x, y), text, font=f, fill=color)

    center(110, "♛ XERZZ SHOP", 75, "#58C7FF")
    center(220, "PREMIUM • FAST • SECURE", 27, "#8CBDEB")

    draw.rounded_rectangle(
        (260, 330, 820, 430),
        radius=40,
        fill="#0C3157",
        outline="#168CFF",
        width=3
    )
    center(355, "✓ PAYMENT SUCCESS", 37, "#58C7FF")
    center(485, "ТӨЛЕМ СӘТТІ!", 58, "#FFFFFF")

    draw.rounded_rectangle(
        (100, 590, 980, 1030),
        radius=30,
        fill="#091D3B",
        outline="#168CFF",
        width=3
    )

    now = datetime.now(
        ZoneInfo("Asia/Almaty")
    ).strftime("%d.%m.%Y  %H:%M:%S")

    rows = [
        ("ӨНІМ", str(product)),
        ("ТӨЛЕМ СОММАСЫ", f"${float(amount):.2f}"),
        ("УАҚЫТЫ", now)
    ]

    y = 640
    for label, value in rows:
        draw.text(
            (150, y), label,
            font=font(27, True),
            fill="#58C7FF"
        )
        draw.text(
            (150, y + 48), value[:35],
            font=font(38, True),
            fill="#FFFFFF"
        )
        y += 135

    draw.rounded_rectangle(
        (100, 1090, 980, 1200),
        radius=25,
        fill="#0B2B50",
        outline="#168CFF",
        width=3
    )

    center(1105, "ЖАҢА ТАПСЫРЫСТАР ҮШІН", 24, "#8CBDEB")
    center(1145, "@xerzzkingbot", 42, "#58C7FF")
    center(1240, "XERZZ SHOP • РАХМЕТ БЕЗ СКАМ", 27, "#FFFFFF")

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    return BufferedInputFile(
        buffer.getvalue(),
        filename="xerzz_receipt.png"
    )


async def send_group_receipt(
    bot, group_id, product, amount, username
):
    photo = make_receipt(product, amount)

    buyer = f"@{username}" if username else "Клиент"

    caption = (
        f"👑 <b>XERZZ SHOP</b>\n"
        f"📦 Өнім: <b>{product}</b>\n"
        f"👤 : {buyer}\n"
        f"⭐ : Рахмет без скам"
    )

    await bot.send_photo(
        chat_id=group_id,
        photo=photo,
        caption=caption,
        parse_mode="HTML"
    )