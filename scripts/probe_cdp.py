"""连上 Electron 的 CDP，打印页面结构与可见文本，用于定位分析。

用法：python scripts/probe_cdp.py http://127.0.0.1:9333
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright


def main() -> int:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9333"
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        print(f"contexts={len(browser.contexts)}")
        for ci, ctx in enumerate(browser.contexts):
            print(f"\n== context {ci} pages={len(ctx.pages)} ==")
            for pi, page in enumerate(ctx.pages):
                try:
                    title = page.title()
                    url = page.url
                except Exception as e:
                    print(f"  page {pi}: <err {e}>")
                    continue
                print(f"  page {pi}: title={title!r} url={url[:110]!r}")
                try:
                    print("    body text:", repr(page.inner_text("body")[:400]))
                except Exception as e:
                    print("    <no body text>", type(e).__name__)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
