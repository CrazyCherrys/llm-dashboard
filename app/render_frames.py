"""Short-lived, network-blocked Chromium renderer, invoked with a hard process timeout."""
import base64
import json
import sys

from playwright.sync_api import sync_playwright
from checks import inspect_svg, MAX_RESPONSE


def capture(svg):
    svg, checks, _ = inspect_svg(svg, "")
    if not svg or not checks["svg"]:
        raise ValueError("Unsafe SVG")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 960, "height": 640},
                                      service_workers="block", accept_downloads=False)
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(8000)
        # Parse as XML, as the public <img> does: HTML parsing drops aliased xlink references.
        page.route("http://render.invalid/scene.svg", lambda route: route.fulfill(
            content_type="image/svg+xml", body=svg,
            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'"}))
        page.goto("http://render.invalid/scene.svg", wait_until="load")
        cycle = page.evaluate("""() => {
            const svg = document.querySelector('svg');
            svg.style.setProperty('width', '960px', 'important');
            svg.style.setProperty('height', '640px', 'important');
            svg.style.setProperty('overflow', 'hidden', 'important');
            svg.style.setProperty('background', 'white', 'important');
            svg.pauseAnimations();
            const durations = [...svg.querySelectorAll('animate,animateTransform,animateMotion')].map(a => {
                try { return a.getSimpleDuration(); } catch { return 0; }
            });
            document.getAnimations().forEach(a => { a.pause(); durations.push(a.effect.getTiming().duration / 1000); });
            const usable = durations.filter(v => Number.isFinite(v) && v >= 0.5 && v <= 10);
            return usable.length ? Math.min(...usable) : 3;
        }""")
        frames = []
        for fraction in (0, .23, .51, .79):
            seconds = cycle * fraction
            page.evaluate("""seconds => {
                document.querySelector('svg').setCurrentTime(seconds);
                document.getAnimations().forEach(a => { a.pause(); a.currentTime = seconds * 1000; });
            }""", seconds)
            png = page.screenshot(type="png", animations="allow")
            frames.append({"time": seconds, "png": base64.b64encode(png).decode()})
        context.close()
        browser.close()
        return frames


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("SVG too large")
    print(json.dumps(capture(raw.decode())))
