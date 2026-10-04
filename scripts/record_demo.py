"""Record a demo video of the operator working, by actually driving it.

Playwright bundles ffmpeg, so this needs no screen-recording software and no
manual mouse work: it opens the dashboard, starts a real run, and films the
browser while the operator thinks. Nothing is staged — what the video shows is
what happened, including any failure or recovery along the way, which is rather
the point.

It captures the *dashboard*, not the whole desktop. For the finance family that
is the complete picture: the operator's own Chromium is headless and its browser
work is reported into the stream it is filming. The IT family additionally drives
a Qt window that lives outside the browser, so for that one the screenshots in
the evidence bundle are the better record.

    python scripts/record_demo.py                       # finance family
    python scripts/record_demo.py --task it             # IT onboarding
    python scripts/record_demo.py --goal "..."          # anything you like
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import time
import urllib.request
from pathlib import Path

# Progress has to be visible while a ten-minute recording runs; a buffered
# stdout made the first attempt completely opaque.
print = functools.partial(print, flush=True)  # noqa: A001

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

DASHBOARD = "http://127.0.0.1:8780"
OUT_DIR = REPO_ROOT / "artifacts" / "demo"

TASKS = {
    "finance": "The vendor invoices in the shared drive need processing - please take care of them.",
    "it": "Someone new is joining - there's a setup request in the HR inbox. Please get them set up.",
    "reconcile": "Reconcile PO-4490 and tell me whether we have been over-billed.",
}

#: A person watching needs a beat to read each thing. These pauses are for the
#: viewer, not for the software.
BEAT = 2.5


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{DASHBOARD}{path}", timeout=20) as response:
        return json.load(response)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Record a demo of a live run.")
    parser.add_argument("--task", choices=sorted(TASKS), default="finance")
    parser.add_argument("--goal", default="", help="Override the request text.")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to wait for the run.")
    parser.add_argument("--approve", default="Checked and approved. Proceed.",
                        help="What to answer if the operator stops for a person.")
    args = parser.parse_args()
    goal = args.goal or TASKS[args.task]

    try:
        config = _get("/api/config")
    except Exception as exc:  # noqa: BLE001
        print(f"Dashboard not reachable at {DASHBOARD}: {exc}")
        print("Start it first:  python -m centralign.cli serve")
        return 1
    print(f"provider {config['provider'].split('/')[0]} · {config['tool_count']} tools "
          f"· surfaces {', '.join(config['surfaces'])}")

    # One directory PER RECORDING. An earlier version wiped the whole demo folder
    # on start, so recording a second task destroyed the first one's video -- and
    # because it half-deleted a file that was still being encoded, it left a
    # truncated video that still reported a valid duration. Never again.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_DIR / f"{args.task}-{stamp}"
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
        page.on("pageerror", lambda e: print(f"  [page error] {e}"))

        # 1. the landing page: what this thing is
        await page.goto(f"{DASHBOARD}/", wait_until="networkidle")
        await page.wait_for_timeout(int(BEAT * 1600))
        await page.evaluate("window.scrollTo({top: 620, behavior: 'smooth'})")
        await page.wait_for_timeout(int(BEAT * 1200))
        await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        await page.wait_for_timeout(1200)

        # 2. into the operator
        await page.goto(f"{DASHBOARD}/operator", wait_until="networkidle")
        await page.wait_for_timeout(int(BEAT * 1000))

        # 3. the request, typed rather than pasted, so it reads as a person asking
        await page.fill("#goal", "")
        await page.type("#goal", goal, delay=18)
        await page.wait_for_timeout(1200)

        started = time.time()
        await page.click("#start")
        print(f"run started: {goal[:70]}...")

        # 4. follow it. Poll the API rather than the DOM so the video is never
        #    disturbed by scripted scrolling the viewer did not ask for.
        # Identify the run through the API. Reading the page's `currentRun`
        # depends on a top-level `let` being reachable from an injected
        # evaluation, which it is not reliably -- and when that silently returned
        # nothing, the recorder polled forever and never noticed the run had
        # stopped for approval.
        run_id = None
        for _ in range(20):
            await page.wait_for_timeout(1000)
            try:
                runs = _get("/api/runs")["runs"]
            except Exception:  # noqa: BLE001
                continue
            fresh = [r for r in runs if r["updated_at"] >= started - 5]
            if fresh:
                run_id = fresh[0]["run_id"]
                break
        if run_id is None:
            print("  could not identify the run; recording the page anyway")
        else:
            print(f"  run_id {run_id}")

        answered = False
        last = ""
        while run_id and time.time() - started < args.timeout:
            await page.wait_for_timeout(2500)
            try:
                state = _get(f"/api/runs/{run_id}")
            except Exception:  # noqa: BLE001
                continue
            status = state["state"]["status"]
            if status != last:
                print(f"  {int(time.time()-started):>4}s  {status}")
                last = status

            if status == "awaiting_human" and not answered:
                # Let the pause be visible before answering it: the approval gate
                # is one of the more interesting things in the recording.
                await page.wait_for_timeout(int(BEAT * 2200))
                box = page.locator("#answer")
                if await box.count():
                    await box.fill(args.approve)
                    await page.wait_for_timeout(900)
                    button = page.locator("#yes")
                    if not await button.count():
                        button = page.locator("#send")
                    await button.click()
                    answered = True
                    print("  answered the operator's request")
                continue

            if status in {"completed", "partial", "failed", "aborted"}:
                print(f"  finished: {status}")
                break

        # 5. let the verdict and evidence land
        await page.wait_for_timeout(int(BEAT * 1800))
        await page.evaluate(
            "() => { const p = document.querySelectorAll('.pane')[2]; if (p) p.scrollTo({top: 0, behavior:'smooth'}); }"
        )
        await page.wait_for_timeout(int(BEAT * 1600))

        video = page.video
        await context.close()          # the file is only finalised on close
        await browser.close()

        if video:
            raw = Path(await video.path())
            final = out_dir / f"centralign-operator-demo-{args.task}.webm"
            raw.replace(final)
            size = final.stat().st_size / 1_000_000
            print(f"\nvideo: {final}  ({size:.1f} MB, {int(time.time()-started)}s of run)")
        else:
            print("no video was captured")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
