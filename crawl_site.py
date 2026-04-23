import asyncio
import os
import re
import time
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright
import aiohttp

MAX_RUNTIME_SECONDS = 5.5 * 3600  # 5.5 hours

# Utility to sanitize filenames
INVALID_CHARS = r'[^a-zA-Z0-9._-]'
def sanitize_filename(name):
    return re.sub(INVALID_CHARS, '_', name)

def get_local_path(url, base_url, output_dir):
    parsed = urlparse(url)
    rel_path = parsed.path.lstrip('/') or 'index.html'
    if rel_path.endswith('/'):
        rel_path += 'index.html'
    local_path = os.path.join(output_dir, sanitize_filename(parsed.netloc), rel_path)
    return local_path

async def download_file(session, url, base_url, output_dir, errors):
    local_path = get_local_path(url, base_url, output_dir)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                with open(local_path, 'wb') as f:
                    f.write(await resp.read())
    except Exception as e:
        errors.append(f"download {url}: {e}")

LOG_INTERVAL = 50  # print progress every N pages

async def crawl(url, base_url, output_dir, visited, session, page, start_time, errors, max_depth=2, depth=0):
    if url in visited or depth > max_depth:
        return
    if time.monotonic() - start_time >= MAX_RUNTIME_SECONDS:
        if len(visited) == 0 or len(visited) % LOG_INTERVAL != 0:
            print(f"[{len(visited)} pages] Timeout reached (5.5 hours). Stopping crawl.")
        return
    visited.add(url)
    if len(visited) % LOG_INTERVAL == 0:
        elapsed = (time.monotonic() - start_time) / 3600
        print(f"[{len(visited)} pages | {elapsed:.2f}h] Last: {url}")
    try:
        await page.goto(url, wait_until='networkidle')
        content = await page.content()
        local_path = get_local_path(url, base_url, output_dir)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'w', encoding='utf-8') as f:
            f.write(content)
        # Download images and other static files
        elements = await page.query_selector_all('img,link[rel="stylesheet"],script[src]')
        for el in elements:
            src = await el.get_attribute('src') or await el.get_attribute('href')
            if src:
                abs_url = urljoin(url, src)
                if abs_url.startswith(base_url):
                    await download_file(session, abs_url, base_url, output_dir, errors)
        # Find internal links
        links = await page.eval_on_selector_all('a[href]', 'els => els.map(e => e.href)')
        for link in links:
            if link.startswith(base_url) and link not in visited:
                await crawl(link, base_url, output_dir, visited, session, page, start_time, errors, max_depth, depth+1)
    except Exception as e:
        errors.append(f"crawl {url}: {e}")

async def main(start_url, output_dir, max_depth=2):
    visited = set()
    errors = []
    base_url = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(start_url))
    os.makedirs(output_dir, exist_ok=True)
    start_time = time.monotonic()
    print(f"Starting crawl: {start_url} (max_depth={max_depth})")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        async with aiohttp.ClientSession() as session:
            await crawl(start_url, base_url, output_dir, visited, session, page, start_time, errors, max_depth)
        await browser.close()
    elapsed = time.monotonic() - start_time
    print(f"Crawl finished. Elapsed: {elapsed/3600:.2f}h | Pages: {len(visited)} | Errors: {len(errors)}")
    if errors:
        print("--- Errors summary ---")
        for err in errors:
            print(f"  {err}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python crawl_site.py <start_url> <output_dir> [max_depth]")
        sys.exit(1)
    start_url = sys.argv[1]
    output_dir = sys.argv[2]
    max_depth = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    asyncio.run(main(start_url, output_dir, max_depth))
