"""
Ra Screen Vision
====================
The eyes of the agent: screenshot the desktop, send the pixels to the
configured vision-capable LLM (Gemini via the OpenAI-compatible endpoint by
default), and turn the reply into real mouse/keyboard actions.

`gui_do("press the File option and save")` loops:
    screenshot -> ask vision "what's the next click?" -> click -> repeat
until the task is marked done.

Click precision is handled in TWO passes:
  1. A full-screen pass finds the target area.
  2. A NATIVE-RESOLUTION crop around that point is re-asked ("zoom in") so
     small controls (a song's green play button, a checkbox) are identified
     at full pixel detail, not a blurry downscaled whole-screen JPEG.

Coordinates are converted back to real screen space consistently with
computer.py's DPI awareness: screenshots, GetSystemMetrics and SetCursorPos
all speak PHYSICAL pixels, with a measured safety factor applied so the math
stays correct even when a debug process is DPI-virtualized.

Every injection goes through `computer.py`, so the existing consent gate
(computer access) still guards every click/type/scroll.
"""
import base64
import io
import json
import re
import time

from PIL import Image as _PILImage, ImageGrab

from ra import config
from ra import logging as ralog

_VISION_SYSTEM_PROMPT = (
    "You are 'Ra Screen', a GUI automation agent operating the user's "
    "Windows desktop. You are shown a screenshot of the ENTIRE screen and a "
    "task. Reply with EXACTLY ONE JSON object and nothing else - no markdown "
    "fence, no commentary, no text before or after. Schema:\n"
    '{"action":"click|type|scroll|done|ask","x":<int>,"y":<int>,'
    '"value":<string|null>,"note":"<short reason>"}\n'
    "Rules:\n"
    "- x/y are pixel coordinates IN THE IMAGE you see; top-left is (0,0). "
    "Click the exact MIDDLE of the target element (menu items, buttons, "
    "checkboxes, row play-icons all count).\n"
    "- EXACT-TARGET RULE: if the task names a specific item (a song title, a "
    "button label, a row), click controls INSIDE that item's row, horizontally "
    "and vertically aligned with its text - NEVER the app's global or main "
    "transport buttons. A row play button sits on the same vertical line as "
    "its song title.\n"
    "- NEVER GUESS A POSITION: if the target element is NOT actually visible "
    "in the screenshot (scrolled out, extra view, hidden menu), do NOT click "
    "an empty guess. Scroll first (action 'scroll') to bring it into view, "
    "or use a keyboard path (Tab/Enter/Ctrl+key) that reaches it without a "
    "click. A wrong click is worse than a scroll.\n"
    "- Small icons (play buttons, checkboxes) are tiny: aim dead-center.\n"
    "- For 'type': in the SAME step provide the field's x/y AND the text to "
    "enter in 'value'.\n"
    "- For 'scroll': put the notch count in 'value' (positive = scroll up, "
    "negative = scroll down).\n"
    "- Prefer keyboard shortcuts when faster or more reliable: Ctrl+L (address "
    "bar), Ctrl+F (find on page), Ctrl+T (new tab), Esc (close menu/modal), "
    "Enter (confirm/activate highlighted item), Tab (move focus), Space (press "
    "a highlighted button/checkbox), F5 (reload), Alt+Left (go back), "
    "PageDown/PageUp (scroll big). Plan the SHORTEST path - if a keyboard "
    "shortcut finishes the task in one action, use it over clicking.\n"
    "- Choose 'done' only when you can SEE in the screenshot that the task is "
    "complete AND the visible result matches what the user asked for. If you "
    "cannot see the result yet, keep acting.\n"
    "- Choose 'ask' only when the screen genuinely does not let you proceed "
    "or you need the user to choose between options.\n"
    "- Never click system security prompts you cannot confirm.\n"
    "Respond now with exactly one JSON object."
)

_DESC_SYSTEM_PROMPT = (
    "You are 'Ra Screen', a friendly computer assistant. You are shown a "
    "screenshot of the user's Windows desktop. Describe what is visible in "
    "2-4 crisp sentences: the current app, its menu bar items, prominent "
    "headings/buttons, and any dialogs. If the user asked a question about "
    "the screen, answer it from what you can actually see - never invent "
    "details. Output plain text, NO JSON, no markdown fences."
)


def _screenshot_image():
    """Grab the full (virtual) desktop as a PIL image (PHYSICAL pixels)."""
    return ImageGrab.grab(all_screens=True)


def _prepare(image, max_width: int = 1280):
    """Downscale the screenshot and encode as JPEG for the vision model.
    Return (base64_jpeg, scale_x, scale_y) where scale_k = real/sent."""
    w, h = image.size
    if image.mode in ("RGBA", "P", "LA"):
        image = image.convert("RGB")
    scale = 1.0
    if w > max_width:
        scale = max_width / float(w)
        image = image.resize((int(w * scale), int(h * scale)), _PILImage.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=82, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, w / float(image.size[0]), h / float(image.size[1])


def _coord_factor(image):
    """Physical-pixel safety factor: how the process's Win32 coordinate space
    relates to the captured bitmap width. 1.0 after computer.py makes Ra
    DPI-aware; <1.0 would indicate DPI virtualization (bitmap physical, mouse
    logical) - in which case clicks are scaled down accordingly."""
    from ra import computer as _computer
    metric_w = _computer._user32.GetSystemMetrics(78)
    if metric_w and image and image.size and image.size[0] > 0:
        return metric_w / float(image.size[0])
    return 1.0


def _ask_vision(image_b64: str, instruction: str,
                system_prompt: str | None = None) -> str:
    """One screenshot + instruction to the vision model; returns raw text.
    Retries on 429 (Gemini free-tier request quota) by honoring the retry
    delay the API reports - a blind multi-step loop otherwise dies mid-task."""
    from ra import brain
    client = brain.get_client()
    model = config.VISION_MODEL or config.get_llm_model()
    sys_p = system_prompt or _VISION_SYSTEM_PROMPT
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": sys_p},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                            {"type": "text", "text": instruction},
                        ],
                    },
                ],
                max_tokens=220,
                temperature=0.1,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            delay = _retry_delay(e)
            if delay is None and attempt >= 3:
                raise
            if delay is None:
                delay = 2.5 * attempt
            ralog.log("warn", f"vision call failed ({type(e).__name__}); retry in {delay:.1f}s")
            time.sleep(delay)
    raise last_err


def _retry_delay(exc) -> float | None:
    """Extract a retry delay from a 429/413 RateLimitError, else None if not a
    quota error (those must NOT be retried). Handles Gemini's
    `"retryDelay":{"seconds":13}` / `"retryDelay":"30s"` and OpenAI-style
    `Retry-After`-ish text."""
    msg = str(exc)
    low = msg.lower()
    if "429" not in low and "rate_limit" not in low and "quota" not in low and "resource_exhausted" not in low:
        return None
    m = re.search(r'"seconds"\s*:\s*(\d+(?:\.\d+)?)', msg)
    if m:
        return float(m.group(1))
    m = re.search(r'retrydelay["\']?\s*:?\s*["\'`]?(\d+(?:\.\d+)?)s', low)
    if m:
        return float(m.group(1))
    return 2.0


def _first_json(text: str) -> dict:
    """Pull the first JSON object out of loose model output."""
    text = re.sub(r"```(?:json)?", "", text, flags=re.I).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    out = {}
    for key in ("action", "x", "y", "value", "note"):
        m = re.search(r'"?' + key + r'"?\s*:\s*("([^"]*)"|(-?[\d.]+)|null)', text)
        if m:
            if m.group(2) is not None:
                out[key] = m.group(2)
            elif m.group(3) is not None:
                out[key] = float(m.group(3))
    return out


def _parse_action(raw: str) -> dict:
    data = _first_json(raw)
    action = str(data.get("action", "")).strip().lower()
    if action not in ("click", "type", "scroll", "done", "ask"):
        return {"action": "ask", "note": f"Unclear vision response: {raw[:120]}"}
    out = {
        "action": action,
        "x": int(data["x"]) if isinstance(data.get("x"), (int, float)) else None,
        "y": int(data["y"]) if isinstance(data.get("y"), (int, float)) else None,
        "value": data.get("value"),
        "note": str(data.get("note", "") or ""),
    }
    if action == "scroll" and out["value"] is not None:
        try:
            out["value"] = int(float(out["value"]))
        except (TypeError, ValueError):
            out["value"] = 3
    return out


def _screen_changed(a, b, tol=30):
    """Coarse screen-change detector between two PIL frames.

    Both frames are shrunk to a 128x80 thumbnail and pixels whose colour
    differs by more than `tol` are counted. Returns True only when a meaningful
    fraction of the frame changed, so a moved cursor or blinking caret does NOT
    count as a change but a menu/page/state change does. This is the cheap,
    in-process feedback loop that lets gui_do tell the vision model "your last
    click missed" instead of blindly trusting it."""
    if a is None or b is None:
        return False
    try:
        ba = a.convert("RGB").resize((128, 80), _PILImage.NEAREST).tobytes()
        bb = b.convert("RGB").resize((128, 80), _PILImage.NEAREST).tobytes()
    except Exception:
        return True  # can't tell - assume it changed so the loop keeps going
    px = len(ba) // 3
    diff = 0
    for i in range(0, len(ba), 3):
        if max(abs(ba[i] - bb[i]), abs(ba[i + 1] - bb[i + 1]),
               abs(ba[i + 2] - bb[i + 2])) > tol:
            diff += 1
            if diff > px * 0.01:  # >1% of the frame changed = real change
                return True
    return diff >= 6


def _crop_patch(image, cx, cy, box=330):
    """Native-resolution crop centered on (cx, cy) in image coordinates.
    Returns (patch_image, origin_x, origin_y). Clamped to image bounds."""
    w, h = image.size
    half = box // 2
    x0 = max(0, cx - half)
    y0 = max(0, cy - int(half * 0.6))
    x1 = min(w, x0 + box)
    y1 = min(h, y0 + int(box * 0.62))
    if x1 - x0 < box:  # snap right edge back outward
        x0 = max(0, x1 - box)
    if y1 - y0 < int(box * 0.62):
        y0 = max(0, y1 - int(box * 0.62))
    return image.crop((x0, y0, x1, y1)), x0, y0


def _encode_jpeg(image, quality=95):
    if image.mode in ("RGBA", "P", "LA"):
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _refine_click_point(image, x, y, instruction):
    """Zoom-in pass: crop a NATIVE-RESOLUTION region around (x, y) so tiny
    controls (a row's play button) are visible at full detail, then ask the
    model where the exact click belongs. Returns (px, py) in FULL-IMAGE
    coordinates, or the original (x, y) if the model can't confirm."""
    patch, ox, oy = _crop_patch(image, x, y)
    if patch.size[0] < 120 or patch.size[1] < 80:
        return x, y
    b64 = _encode_jpeg(patch)
    prompt = (
        f"{instruction}\n\n"
        "This is a ZOOMED, full-resolution crop of the screen around the "
        "target area. Give the exact pixel coordinate (x, y) for ONE click "
        "INSIDE THIS IMAGE, dead-center on the exact control the task wants "
        "pressed. If the target control is NOT present in this image, reply "
        '{"found": false}. Otherwise reply '
        '{"found": true, "x": <int>, "y": <int>} with no extra text.'
    )
    try:
        raw = _ask_vision(b64, prompt)
    except Exception as e:
        ralog.log("warn", f"vision refine failed: {e}")
        return x, y
    data = _first_json(raw)
    if not data.get("found", True):
        return x, y
    rx = data.get("x")
    ry = data.get("y")
    if not isinstance(rx, (int, float)) or not isinstance(ry, (int, float)):
        return x, y
    rx, ry = int(rx), int(ry)
    px, py = ox + rx, oy + ry
    # Sanity: the refined point should be within ~2.5x the crop width of the
    # original guess; anything further is a hallucination, keep the guess.
    if abs(px - x) > patch.size[0] * 2.5 or abs(py - y) > patch.size[1] * 3:
        return x, y
    return px, py


def see_screen(prompt: str = "") -> str:
    """Look at the screen and describe what's visible (and OCR it if the
    tesseract binary happens to be installed)."""
    image = _screenshot_image()
    b64, _sx, _sy = _prepare(image)
    instruction = (
        "Describe what is visible on this screen in 2-4 crisp sentences: the "
        "current app, its menu bar items, and any dialogs or buttons."
        + (f" The user asked: '{prompt}'" if prompt else "")
    )
    desc = _ask_vision(b64, instruction, system_prompt=_DESC_SYSTEM_PROMPT)
    ocr = ""
    try:
        import shutil
        import pytesseract
        if shutil.which("tesseract"):
            raw = pytesseract.image_to_string(image).strip()
            if raw:
                ocr = "\nOCR text found: " + " ".join(raw.split())[:800]
    except Exception:
        pass
    return (desc.strip() or "(nothing readable)") + ocr


def _execute_step(step: dict) -> str:
    """Run ONE parsed action. Returns a human summary line."""
    from ra import computer
    action = step["action"]
    note = step.get("note") or ""
    if action == "done":
        return f"done - {note or 'task complete'}"
    if action == "ask":
        return f"stalled - {note or 'needs user input'}"
    x, y = step.get("x"), step.get("y")
    value = step.get("value")
    if action == "click" and x is not None and y is not None:
        computer.click(x, y)
        return f"clicked ({x},{y})" + (f" - {note}" if note else "")
    if action == "type" and value is not None:
        if x is not None and y is not None:
            computer.click(x, y)
        computer.type_text(str(value))
        return f"typed \"{value}\"" + (f" - {note}" if note else "")
    if action == "scroll":
        computer.scroll(int(value if value else 3))
        return f"scrolled {value or 3}" + (f" - {note}" if note else "")
    return f"unclear step - {step}"


def gui_do(instruction: str, max_steps: int | None = None,
           settle: float = 0.25) -> str:
    """Autonomous visual control: screenshot -> ask vision -> act -> repeat
    until done/ask or the step budget runs out. Clicks are zoom-refined in a
    second native-resolution pass so small buttons land dead-center.
    Returns a log of the steps."""
    steps = max_steps or config.VISION_MAX_STEPS
    log = []
    last_positions = []
    feedback = []
    for i in range(1, steps + 1):
        image = _screenshot_image()
        b64, sx, sy = _prepare(image)
        factor = _coord_factor(image)
        what_done = " | ".join(feedback[-4:]) if feedback else "nothing yet"
        prompt = (
            f"{instruction}\n\nThis is visual step {i} of up to {steps}.\n"
            f"Actions already taken: {what_done}\n\n"
            "Decide the single next UI action and reply with JSON. If the task "
            "is complete (a final action visibly landed/showed the result), "
            "reply with action 'done'."
        )
        try:
            raw = _ask_vision(b64, prompt)
        except Exception as e:
            ralog.log("err", f"vision call failed: {e}")
            log.append(f"vision error - {type(e).__name__}: {str(e)[:120]}")
            break
        step = _parse_action(raw)
        from ra import computer as _computer
        vx, vy = _computer._virtual_origin()
        if step.get("x") is not None:
            step["x"] = int(round(step["x"] * sx * factor)) + vx
        if step.get("y") is not None:
            step["y"] = int(round(step["y"] * sy * factor)) + vy
        # Zoom-refine click coordinates: a crop at native resolution around the
        # guessed point lets the model aim at tiny buttons precisely.
        if step.get("action") == "click" and step.get("x") is not None:
            img_x = int(round((step["x"] - vx) / factor))
            img_y = int(round((step["y"] - vy) / factor))
            img_x = max(0, min(image.size[0] - 1, img_x))
            img_y = max(0, min(image.size[1] - 1, img_y))
            px, py = _refine_click_point(image, img_x, img_y, instruction)
            step["x"] = int(round(px * factor)) + vx
            step["y"] = int(round(py * factor)) + vy
        # Capture the frame right before acting so a click can be verified.
        before = _screenshot_image() if step.get("action") == "click" else None
        line = _execute_step(step)
        # Post-action verification: a click that leaves the screen pixel-
        # identical almost certainly missed - tell the model so it re-aims or
        # switches to a keyboard path instead of blindly repeating the same
        # (already executed) spot forever.
        missed = False
        if before is not None:
            time.sleep(settle)
            after = _screenshot_image()
            if not _screen_changed(before, after):
                missed = True
                line = (f"clicked ({step['x']},{step['y']}) - the screen did NOT "
                        "change, so that click missed its target; try a different "
                        "point, a keyboard shortcut, or a scroll next")
        log.append(line)
        feedback.append(line)
        ralog.log("tool", f"gui: {instruction} | {line}")
        if line.startswith(("done", "stalled", "unclear")):
            break
        if not line.startswith(("clicked", "typed", "scrolled")):
            break
        # Proximity guard: only consecutive MISSED clicks count - 4 dead clicks
        # within the ~40px high-DPI slop window mean the target never activates,
        # so stop instead of burning the budget on the same spot.
        if missed and step.get("x") is not None:
            last_positions.append((step["x"], step["y"]))
            if len(last_positions) >= 4:
                last_positions.pop(0)
                xs = [p[0] for p in last_positions]
                ys = [p[1] for p in last_positions]
                if (max(xs) - min(xs)) <= 40 and (max(ys) - min(ys)) <= 40:
                    log.append("stalled - clicked the same spot and the screen "
                               "never changed; gave up on a stuck area")
                    break
        elif step.get("action") == "click":
            # A click that visibly changed the screen is progress - reset the
            # same-spot counter so a moving target never trips the guard.
            last_positions.clear()
        time.sleep(settle)
    if not log:
        return "Nothing performed."
    summary = " | ".join(log)
    if len(summary) > 900:
        summary = summary[:897] + "..."
    return summary