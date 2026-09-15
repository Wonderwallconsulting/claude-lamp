"""Small, readable RGB avatar frames for a 32 x 32 LED matrix.

Cyan robot silhouette (rounded head + antenna + eyes/mouth) with phase-driven
idle life and 90s-comic gesticulation. EXPRESSIONS API is stable.
"""

from PIL import Image, ImageDraw


EXPRESSIONS = ("idle", "blink", "thinking", "happy", "wink", "error", "speaking", "notify")
SIZE = 32
CYAN = (0, 180, 255)
WHITE = (160, 245, 255)
AMBER = (255, 190, 40)
RED = (255, 55, 35)
CHEEK = (40, 160, 220)
PUPIL = (0, 90, 140)


def _pulse(base, phase):
    """Soft glow pulse on outline brightness (phase-driven)."""
    bump = (0, 18, 36, 50, 36, 18, 0, -12)[phase % 8]
    r, g, b = base
    return (
        max(0, min(255, r + bump // 3)),
        max(0, min(255, g + bump)),
        max(0, min(255, b + bump)),
    )


def _face_offset(expression: str, phase: int):
    """Whole-face bob/sway (+ expression-specific bounce/shake)."""
    # Idle: obvious ±1 bob + slower sway so a held idle never freezes.
    bob = (0, -1, 0, 1)[phase % 4]
    sway = (0, 1, 0, -1)[(phase // 2) % 4]
    if expression == "idle":
        return sway, bob
    if expression == "happy":
        # Happy bounce: exaggerate vertical motion.
        return 0, (0, -2, 0, -1)[phase % 4]
    if expression == "error":
        # Panic shake.
        return (1, -1, 1, -1)[phase % 4], (0, 1, -1, 0)[phase % 4]
    if expression == "speaking":
        return 0, (0, -1, 0, 0)[phase % 4]
    if expression == "notify":
        return 0, (0, -1, 0, -1)[phase % 4]
    if expression == "thinking":
        return -1, 0  # look-aside lean
    return 0, bob // 2  # blink / wink: tiny residual life


def _antenna_tip(expression: str, phase: int, ox: int, oy: int):
    """Antenna bobble / wiggle relative to the stalk."""
    if expression == "thinking":
        tip_dx = (1, 2, 1, 0)[phase % 4]
    elif expression == "error":
        tip_dx = (2, -2, 1, -1)[phase % 4]
    elif expression == "happy":
        tip_dx = (0, 1, 0, -1)[phase % 4]
    else:
        tip_dx = (0, 1, 0, -1)[phase % 4]
    tip_dy = (0, -1, 0, 0)[phase % 4] if expression in ("idle", "happy", "speaking") else 0
    return 14 + ox + tip_dx, 1 + oy + tip_dy, 17 + ox + tip_dx, 2 + oy + tip_dy


def _draw_head(draw, color, ox, oy, glow=False, phase=0):
    outline = _pulse(color, phase) if glow else color
    # Soft outer glow pulse (dimmer second outline) — sparse, readable at 32x32.
    if glow and phase % 8 in (2, 3, 4):
        dim = tuple(max(0, c // 3) for c in outline)
        draw.rounded_rectangle((2 + ox, 4 + oy, 29 + ox, 28 + oy), radius=6, outline=dim, width=1)
    draw.rounded_rectangle((3 + ox, 5 + oy, 28 + ox, 27 + oy), radius=5, outline=outline, width=2)


def _draw_antenna(draw, color, tip_color, ox, oy, expression, phase):
    draw.line((15 + ox, 2 + oy, 15 + ox, 5 + oy), fill=color, width=2)
    tip = _antenna_tip(expression, phase, ox, oy)
    draw.rectangle(tip, fill=tip_color)


def _draw_cheeks(draw, ox, oy, on: bool):
    if not on:
        return
    for x in (7, 24):
        draw.point((x + ox, 18 + oy), fill=CHEEK)
        draw.point((x + ox, 19 + oy), fill=CHEEK)


def _draw_motion_lines(draw, color, ox, oy, expression, phase):
    """Sparing comic speed lines (error panic / notify pop)."""
    if expression == "error" and phase % 2 == 0:
        draw.point((1, 12 + oy), fill=color)
        draw.point((1, 16 + oy), fill=color)
        draw.point((30, 12 + oy), fill=color)
        draw.point((30, 16 + oy), fill=color)
    elif expression == "notify" and phase % 2 == 0:
        draw.point((2 + ox, 8 + oy), fill=AMBER)
        draw.point((29 + ox, 8 + oy), fill=AMBER)


def _idle_blinking(phase: int) -> bool:
    # Soft blink cycle ~ every 5s at 3 fps (phase 14 of 16).
    return phase % 16 == 14


def _draw_eyes(draw, expression, phase, ox, oy):
    left, right = 8 + ox, 20 + ox
    top = 11 + oy

    if expression == "error":
        for x in (left, right):
            draw.line((x, top, x + 3, top + 4), fill=WHITE, width=2)
            draw.line((x + 3, top, x, top + 4), fill=WHITE, width=2)
        return

    if expression == "blink" or (expression == "idle" and _idle_blinking(phase)):
        for x in (left, right):
            draw.line((x - 1, top + 2, x + 4, top + 2), fill=WHITE, width=2)
        return

    if expression == "wink":
        # Open left eye + closed right + smirk handled in mouth.
        draw.rounded_rectangle((left, top, left + 3, top + 5), radius=1, fill=WHITE)
        draw.point((left + 1, top + 2), fill=PUPIL)
        draw.line((right - 1, top + 2, right + 4, top + 2), fill=WHITE, width=2)
        # Raised-brow feel over open eye.
        draw.line((left - 1, top - 2, left + 4, top - 2), fill=WHITE, width=1)
        return

    if expression == "happy":
        for x in (left, right):
            draw.line((x - 1, top + 3, x + 1, top, x + 4, top + 3), fill=WHITE, width=2)
        return

    # Base open eyes (idle / thinking / speaking / notify) with comic exaggeration.
    if expression == "notify":
        # Wide eyes.
        for x in (left, right):
            draw.ellipse((x - 1, top - 1, x + 4, top + 6), fill=WHITE)
            draw.point((x + 1, top + 2), fill=PUPIL)
        return

    if expression == "thinking":
        # Look-aside + raised brow feel on the far eye.
        drift = 1 + (phase % 3)  # push pupils/eyes rightward
        for i, x in enumerate((left, right)):
            ex = x + drift
            draw.rounded_rectangle((ex, top, ex + 3, top + 5), radius=1, fill=WHITE)
            draw.point((ex + 2, top + 2), fill=PUPIL)
        draw.line((right + drift - 1, top - 2, right + drift + 4, top - 3), fill=WHITE, width=1)
        return

    if expression == "speaking":
        # Exaggerated ovals that squash a little with the talk beat.
        squash = (0, 1, 0, -1)[phase % 4]
        for x in (left, right):
            draw.ellipse((x, top + squash, x + 3, top + 5 - squash), fill=WHITE)
            draw.point((x + 1, top + 2), fill=PUPIL)
        return

    # Idle (open): soft pupil drift.
    pdx = (-1, 0, 1, 0)[phase % 4]
    pdy = (0, 0, 0, 1)[(phase // 2) % 4]
    for x in (left, right):
        draw.rounded_rectangle((x, top, x + 3, top + 5), radius=1, fill=WHITE)
        draw.point((x + 1 + pdx, top + 2 + pdy), fill=PUPIL)


def _draw_mouth(draw, expression, phase, ox, oy):
    mx, my = ox, oy
    if expression == "error":
        draw.line((11 + mx, 23 + my, 14 + mx, 20 + my, 17 + mx, 20 + my, 20 + mx, 23 + my),
                  fill=WHITE, width=2)
        return
    if expression in ("happy",):
        draw.line((10 + mx, 20 + my, 12 + mx, 23 + my, 19 + mx, 23 + my, 21 + mx, 20 + my),
                  fill=WHITE, width=2)
        return
    if expression == "wink":
        # Smirk: asymmetric grin.
        draw.line((11 + mx, 21 + my, 14 + mx, 23 + my, 20 + mx, 20 + my), fill=WHITE, width=2)
        return
    if expression == "thinking":
        draw.line((13 + mx, 22 + my, 18 + mx, 22 + my), fill=WHITE, width=2)
        # Thought speck beside the head.
        draw.point((23 + mx, 20 + my + phase % 3), fill=WHITE)
        return
    if expression == "speaking":
        # Rubbery big talk shapes (Monkey Island energy).
        shapes = (
            (11, 21, 20, 23),   # flat
            (10, 19, 21, 24),   # wide open
            (12, 18, 19, 25),   # tall O
            (11, 20, 20, 24),   # mid
        )
        box = shapes[phase % 4]
        draw.rounded_rectangle(
            (box[0] + mx, box[1] + my, box[2] + mx, box[3] + my),
            radius=2, fill=WHITE,
        )
        return
    if expression == "notify":
        draw.line((12 + mx, 22 + my, 19 + mx, 22 + my), fill=WHITE, width=2)
        return
    # Idle / blink: gentle smile.
    draw.line((11 + mx, 21 + my, 13 + mx, 22 + my, 18 + mx, 22 + my, 20 + mx, 21 + my),
              fill=WHITE, width=1)


def _draw_notify_bang(draw, phase, ox, oy):
    if phase % 2 != 0:
        return
    x = 25 + ox
    draw.line((x, 7 + oy, x, 12 + oy), fill=AMBER, width=2)
    draw.point((x, 14 + oy), fill=AMBER)
    draw.point((x - 1, 14 + oy), fill=AMBER)


def render(expression: str, phase: int = 0) -> Image.Image:
    """Render one frame; phase advances idle life and expression animations."""
    if expression not in EXPRESSIONS:
        raise ValueError(f"Unknown expression: {expression!r}")

    image = Image.new("RGB", (SIZE, SIZE), "black")
    draw = ImageDraw.Draw(image)

    if expression == "error":
        color = RED
        tip = WHITE
    elif expression == "notify":
        color = AMBER
        tip = WHITE
    else:
        color = CYAN
        tip = WHITE

    ox, oy = _face_offset(expression, phase)
    glow = expression in ("idle", "happy", "speaking", "notify")

    _draw_head(draw, color, ox, oy, glow=glow, phase=phase)
    _draw_antenna(draw, color, tip, ox, oy, expression, phase)
    _draw_eyes(draw, expression, phase, ox, oy)
    _draw_mouth(draw, expression, phase, ox, oy)

    cheeks_on = (
        expression == "happy"
        or (expression == "speaking" and phase % 2 == 0)
        or (expression == "idle" and phase % 8 == 3)
    )
    _draw_cheeks(draw, ox, oy, cheeks_on)
    _draw_motion_lines(draw, color, ox, oy, expression, phase)

    if expression == "notify":
        _draw_notify_bang(draw, phase, ox, oy)

    return image
