"""Build a video of the desktop application actually being driven.

Playwright films a browser, and the Asset and Access Manager is a Qt window that
lives outside one — so a dashboard recording shows the operator's *reasoning*
about the desktop app but never the window itself. That is the one surface a
viewer most wants to see, because "it drives a real desktop application" is the
claim people are least likely to take on trust.

The operator already screenshots every desktop action as it performs it, so the
frames exist. This renders them as a captioned sequence in a page and films that.
No screen-recording software, and nothing staged: every frame is an image the
operator captured of itself at the moment it acted.

    python scripts/record_desktop_clip.py                  # newest run with desktop shots
    python scripts/record_desktop_clip.py --run run_2026...
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO_ROOT / "artifacts"
OUT_ROOT = ARTIFACTS / "demo"

#: Seconds each frame is held. Long enough to read the form, short enough to watch.
HOLD = 2.6


def caption_for(path: Path, index: int, total: int) -> tuple[str, str]:
    """Turn 'desk-007-click-Create-account.png' into something readable."""
    stem = re.sub(r"^desk-\d+-", "", path.stem)
    verb, _, rest = stem.partition("-")
    target = rest.replace("-", " ").strip()

    if verb == "click":
        title = f"Clicking “{target}”"
        note = "A real button press, through the UI Automation Invoke pattern."
    elif verb == "fill":
        title = "Entering values into the form"
        note = "Fields targeted by their accessible name, the way a person reads a label."
    elif verb == "open":
        title = "Opening the Asset and Access Manager"
        note = "The operator launches the application itself if it is not running."
    elif verb == "read":
        title = "Reading the window back"
        note = "Every write is read back; the status line reports what the app decided."
    elif verb == "inspect":
        title = "Inspecting what is on screen"
        note = "Discovering which controls exist before choosing an action."
    else:
        title = stem.replace("-", " ").capitalize()
        note = ""
    return f"{index}/{total} · {title}", note


def find_run(explicit: str | None) -> Path:
    if explicit:
        path = ARTIFACTS / explicit
        if not list(path.glob("desk-*.png")):
            raise SystemExit(f"no desktop screenshots in {path}")
        return path
    candidates = [d for d in ARTIFACTS.glob("run_*") if list(d.glob("desk-*.png"))]
    if not candidates:
        raise SystemExit(
            "No run has desktop screenshots yet. Run the IT onboarding task on Windows first."
        )
    return max(candidates, key=lambda d: len(list(d.glob("desk-*.png"))))


def build_page(shots: list[Path], run_id: str) -> str:
    """Inline every frame as a data URI, so the page needs no server."""
    slides = []
    for index, shot in enumerate(shots, start=1):
        title, note = caption_for(shot, index, len(shots))
        data = base64.b64encode(shot.read_bytes()).decode("ascii")
        slides.append(
            f'<figure class="slide"><img src="data:image/png;base64,{data}" alt="">'
            f'<figcaption><b>{html.escape(title)}</b>'
            f'{"<span>" + html.escape(note) + "</span>" if note else ""}</figcaption></figure>'
        )

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  *{{box-sizing:border-box;margin:0;}}
  body{{background:#0b0c0f;color:#e9ebef;font:15px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;
    height:100vh;overflow:hidden;}}
  .head{{position:fixed;top:0;left:0;right:0;height:52px;display:flex;align-items:center;gap:12px;
    padding:0 22px;background:#14161a;border-bottom:1px solid #242831;z-index:5;}}
  .mark{{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;font-weight:800;
    font-size:13px;background:linear-gradient(140deg,#7b95ff,#a98bf5);color:#fff;}}
  .head b{{font-size:14px;}} .head span{{color:#6e757f;font-size:12.5px;}}
  .head .run{{margin-left:auto;font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:#6e757f;}}
  .stage{{position:absolute;inset:52px 0 0 0;display:grid;place-items:center;padding:26px 26px 96px;}}
  .slide{{display:none;max-width:100%;max-height:100%;}}
  .slide.on{{display:block;}}
  .slide img{{display:block;max-width:100%;max-height:calc(100vh - 190px);margin:0 auto;
    border:1px solid #242831;border-radius:10px;box-shadow:0 18px 50px -18px rgba(0,0,0,.8);}}
  figcaption{{position:fixed;left:0;right:0;bottom:0;padding:14px 26px 20px;background:#14161a;
    border-top:1px solid #242831;}}
  figcaption b{{display:block;font-size:16px;letter-spacing:-.01em;}}
  figcaption span{{display:block;margin-top:3px;color:#9ea5b0;font-size:13px;}}
  .bar{{position:fixed;left:0;bottom:0;height:2px;background:#7b95ff;width:0;transition:width .3s linear;}}
</style></head><body>
<div class="head"><span class="mark">C</span><b>Asset &amp; Access Manager</b>
  <span>driven by the operator through Windows UI Automation</span>
  <span class="run">{html.escape(run_id)}</span></div>
<div class="stage">{"".join(slides)}</div>
<div class="bar" id="bar"></div>
<script>
  var slides = document.querySelectorAll('.slide');
  window.__total = slides.length;
  window.__show = function (i) {{
    slides.forEach(function (s, n) {{ s.classList.toggle('on', n === i); }});
    document.getElementById('bar').style.width = ((i + 1) / slides.length * 100) + '%';
  }};
  window.__show(0);
</script></body></html>"""


async def main() -> int:
    parser = argparse.ArgumentParser(description="Film the desktop app from a run's screenshots.")
    parser.add_argument("--run", default="", help="Run id under artifacts/. Defaults to the richest.")
    parser.add_argument("--hold", type=float, default=HOLD, help="Seconds per frame.")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()

    run_dir = find_run(args.run or None)
    shots = sorted(run_dir.glob("desk-*.png"))
    print(f"run {run_dir.name}: {len(shots)} desktop frames", flush=True)

    page_file = run_dir / "_desktop_clip.html"
    page_file.write_text(build_page(shots, run_dir.name), encoding="utf-8")

    out_dir = OUT_ROOT / f"desktop-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": args.width, "height": args.height},
            record_video_dir=str(out_dir),
            record_video_size={"width": args.width, "height": args.height},
        )
        page = await context.new_page()
        await page.goto(page_file.as_uri(), wait_until="load")
        await page.wait_for_timeout(1400)

        for index in range(len(shots)):
            await page.evaluate("i => window.__show(i)", index)
            await page.wait_for_timeout(int(args.hold * 1000))
        await page.wait_for_timeout(1600)

        video = page.video
        await context.close()
        await browser.close()

        if video:
            final = out_dir / f"centralign-desktop-{run_dir.name}.webm"
            Path(await video.path()).replace(final)
            print(f"\nvideo: {final}  ({final.stat().st_size/1_000_000:.1f} MB)", flush=True)
    page_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
