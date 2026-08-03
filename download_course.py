"""Download an Open edX course (videos + written content) for offline use.

Built for madrasafree.com but works against any Open edX instance. The output is
a self-contained offline copy of the courseware UI: one page per unit with the
video player and the written blocks inline, a unit strip, breadcrumbs and
previous/next navigation, plus a course outline page.

Two modes:

  Bundle mode (no credentials needed):
    A JSON bundle holding the course tree and the pre-rendered written content is
    extracted from a logged-in browser session, then everything else - videos,
    images, audio - is fetched from the public CDNs.

      python download_course.py --bundle bundle.json --out DIR

  Live mode (needs a logged-in session cookie for the written content):

      python download_course.py course-v1:org+num+run --sessionid VALUE --out DIR

Re-running is cheap: files already downloaded are left alone and only the pages
are rebuilt. Standard library only, apart from yt-dlp (YouTube/HLS videos).
"""

import argparse
import concurrent.futures
import hashlib
import http.cookiejar
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LMS = 'https://courses.madrasafree.com'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

# Hosts whose files are pulled down and rewritten to local paths. Anything else
# (educaplay, quizlet, google docs, CDN scripts) stays a remote link.
LOCALISE_HOSTS = ('amazonaws.com', 'madrasafree.com')

# Same sanitising rules as crawl_site.py: keep Hebrew/Arabic readable, drop what
# Windows and upload-artifact reject.
INVALID_CHARS = r'[\\/:*?"<>|\x00-\x1f]'
MAX_SEGMENT_BYTES = 100

CONTENT_TYPES = ('html', 'problem')
NOT_SAVED = {'discussion': 'דיון בפורום — לא נשמר בעותק האופליין',
             'poll': 'סקר — לא נשמר בעותק האופליין'}

_print_lock = threading.Lock()

# The Windows console defaults to cp1255 here, which cannot encode Arabic
# diacritics that appear in the course titles.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')


def log(*args):
    with _print_lock:
        print(*args, flush=True)


def sanitize_filename(name):
    name = re.sub(INVALID_CHARS, '_', urllib.parse.unquote(str(name))).strip(' .')
    name = re.sub(r'\s+', ' ', name)
    if name in ('', '.', '..'):
        return '_'
    if len(name.encode('utf-8')) > MAX_SEGMENT_BYTES:
        root, ext = os.path.splitext(name)
        ext = ext[:16]
        digest = hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]
        budget = max(MAX_SEGMENT_BYTES - len(ext.encode('utf-8')) - 9, 1)
        root = root.encode('utf-8')[:budget].decode('utf-8', 'ignore') or '_'
        name = f"{root}_{digest}{ext}"
    return name


def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def web_rel(from_dir, to_path):
    """Relative URL from a directory to a path, both relative to the output root."""
    rel = posixpath.relpath(to_path.replace(os.sep, '/'), from_dir.replace(os.sep, '/') or '.')
    return urllib.parse.quote(rel)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class AuthError(SystemExit):
    pass


class _LoginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect to the login page means the cookie is missing or expired."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if '/login' in newurl or '/oauth2/' in newurl:
            raise AuthError(
                "Not authenticated: the LMS redirected to the login page.\n"
                "The session cookie is missing, expired, or for a different host.\n"
                "Log in again in the browser and copy a fresh 'sessionid' value.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    """HTTP client with retries. The cookie is only needed in live mode."""

    def __init__(self, lms=DEFAULT_LMS, cookie_header='', retries=4):
        self.lms = lms.rstrip('/')
        self.cookie_header = cookie_header
        self.retries = retries
        self.opener = urllib.request.build_opener(_LoginRedirectHandler())

    @staticmethod
    def _normalise(url):
        """Percent-encode spaces and other raw characters urllib refuses to send."""
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((
            parts.scheme, parts.netloc,
            urllib.parse.quote(parts.path, safe="/:@!$&'()*+,;=~-._"),
            urllib.parse.quote(parts.query, safe="=&/:@!$'()*+,;~-._"), parts.fragment))

    def _open(self, url):
        url = self._normalise(url)
        headers = {'User-Agent': USER_AGENT, 'Accept': '*/*', 'Referer': self.lms + '/'}
        if self.cookie_header and urllib.parse.urlparse(url).netloc.endswith('madrasafree.com'):
            headers['Cookie'] = self.cookie_header
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=180)

    def _with_retries(self, url, fn):
        delay = 2
        for attempt in range(1, self.retries + 1):
            try:
                return fn(url)
            except AuthError:
                raise
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404) or attempt == self.retries:
                    raise
            except Exception:
                if attempt == self.retries:
                    raise
            time.sleep(delay)
            delay *= 2

    def get_json(self, path, **params):
        url = self.lms + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        return self._with_retries(url, lambda u: json.loads(self._open(u).read().decode('utf-8')))

    def get_text(self, url):
        if url.startswith('/'):
            url = self.lms + url

        def fetch(u):
            resp = self._open(u)
            raw = resp.read()
            return raw.decode(resp.headers.get_content_charset() or 'utf-8', 'replace')

        return self._with_retries(url, fetch)

    def download(self, url, dest, min_bytes=1024):
        """Stream a URL to dest, skipping work already done. Returns dest."""
        if url.startswith('/'):
            url = self.lms + url
        if os.path.exists(dest) and os.path.getsize(dest) >= min_bytes:
            return dest
        os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
        tmp = dest + '.part'

        def fetch(u):
            with self._open(u) as resp, open(tmp, 'wb') as f:
                shutil.copyfileobj(resp, f, 1024 * 256)
            os.replace(tmp, dest)
            return dest

        try:
            return self._with_retries(url, fetch)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# --------------------------------------------------------------------------
# Course tree
# --------------------------------------------------------------------------

BLOCK_FIELDS = 'children,display_name,type,student_view_url,student_view_data'


def fetch_blocks(client, course_id, username):
    """Fetch the whole course tree. Tries the v2 blocks API, falls back to v1."""
    params = {
        'course_id': course_id,
        'username': username,
        'depth': 'all',
        'requested_fields': BLOCK_FIELDS,
        'student_view_data': 'video,html,discussion,problem',
    }
    last_error = None
    for path in ('/api/courses/v2/blocks/', '/api/courses/v1/blocks/'):
        try:
            return client.get_json(path, **params)
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode('utf-8', 'replace')
            last_error = f"{path} -> HTTP {e.code}: {body}"
    raise SystemExit(f"Could not read the course structure.\n  {last_error}")


def build_tree(data):
    blocks = data['blocks']

    def node(block_id, depth=0):
        b = blocks.get(block_id)
        if b is None or depth > 12:
            return None
        return {
            'id': block_id,
            'type': b.get('type'),
            'name': b.get('display_name') or b.get('type') or block_id,
            'student_view_url': b.get('student_view_url'),
            'student_view_data': b.get('student_view_data') or {},
            'children': [c for c in (node(cid, depth + 1) for cid in b.get('children') or []) if c],
        }

    return node(data['root'])


def iter_units(tree):
    """Yield (ancestor_titles, vertical_node) for every unit.

    The trail holds the chapter/section titles above the unit and deliberately
    stops there - the unit contributes its own directory name.
    """
    def walk(node, trail):
        if node['type'] == 'vertical':
            yield trail, node
            return
        for i, child in enumerate(node['children'], 1):
            if child['type'] == 'vertical':
                yield trail, child
            else:
                yield from walk(child, trail + [f"{i:02d} - {child['name']}"])

    yield from walk(tree, [])


def plan_units(tree):
    """Lay out every unit on disk and work out its navigation neighbours."""
    units = []
    for idx, (trail, node) in enumerate(iter_units(tree), 1):
        rel_dir = posixpath.join('content', *[sanitize_filename(t) for t in trail],
                                 sanitize_filename(f"{idx:03d} - {node['name']}"))
        units.append({'index': idx, 'trail': trail, 'node': node, 'rel_dir': rel_dir,
                      'page': rel_dir + '/index.html', 'files': []})

    groups = {}
    for u in units:
        groups.setdefault(tuple(u['trail']), []).append(u)
    for i, u in enumerate(units):
        u['prev'] = units[i - 1] if i else None
        u['next'] = units[i + 1] if i + 1 < len(units) else None
        u['siblings'] = groups[tuple(u['trail'])]
    return units


# --------------------------------------------------------------------------
# Videos
# --------------------------------------------------------------------------

MP4_PREFERENCE = ('fallback', 'desktop_mp4', 'high', 'desktop_webm', 'mobile_high', 'mobile_low')
VIDEO_EXTS = ('.mp4', '.webm', '.mkv', '.mov')


def pick_video_source(svd):
    """Return (kind, url) for the best available source, or (None, None)."""
    encoded = svd.get('encoded_videos') or {}
    for key in MP4_PREFERENCE:
        url = (encoded.get(key) or {}).get('url')
        if url and not url.endswith('.m3u8'):
            return 'direct', url
    for source in svd.get('all_sources') or []:
        if source and not source.endswith('.m3u8'):
            return 'direct', source
    hls = (encoded.get('hls') or {}).get('url')
    if hls:
        return 'ytdlp', hls
    yt = (encoded.get('youtube') or {}).get('url')
    if yt:
        return 'ytdlp', yt
    return None, None


def _existing_video(prefix):
    directory = os.path.dirname(prefix) or '.'
    base = os.path.basename(prefix)
    if not os.path.isdir(directory):
        return None
    for f in sorted(os.listdir(directory)):
        if f.startswith(base + '.') and os.path.splitext(f)[1].lower() in VIDEO_EXTS:
            path = os.path.join(directory, f)
            if os.path.getsize(path) > 10240:
                return path
    return None


def run_ytdlp(url, dest_no_ext):
    """Download via yt-dlp. Returns the resulting path."""
    existing = _existing_video(dest_no_ext)
    if existing:
        return existing
    if not shutil.which('yt-dlp'):
        raise RuntimeError('yt-dlp is not installed; cannot fetch ' + url)
    cmd = ['yt-dlp', '--no-progress', '--no-warnings', '--retries', '5',
           '-f', 'bv*+ba/b', '--merge-output-format', 'mp4',
           '-o', dest_no_ext + '.%(ext)s', url]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-1:]
        raise RuntimeError(f"yt-dlp failed: {tail[0] if tail else 'unknown error'}")
    found = _existing_video(dest_no_ext)
    if not found:
        raise RuntimeError('yt-dlp produced no video file')
    return found


# --------------------------------------------------------------------------
# Written content
# --------------------------------------------------------------------------

REF_RE = re.compile(r'''((?:src|href|data-src)\s*=\s*["'])([^"']+)(["'])''', re.I)


def asset_local_path(url):
    """Map a remote asset URL onto a path under assets/."""
    parsed = urllib.parse.urlparse(url)
    segments = [sanitize_filename(s) for s in parsed.path.split('/') if s] or ['file']
    if parsed.query:
        root, ext = os.path.splitext(segments[-1])
        digest = hashlib.sha1(parsed.query.encode('utf-8')).hexdigest()[:8]
        segments[-1] = f"{root}_{digest}{ext}"
    return posixpath.join('assets', sanitize_filename(parsed.netloc), *segments)


def should_localise(url):
    host = urllib.parse.urlparse(url).netloc
    return any(host == h or host.endswith('.' + h) for h in LOCALISE_HOSTS)


def rewrite_refs(html, base_url, from_dir, assets):
    """Point local assets at their downloaded copies; collect what to fetch."""
    def repl(m):
        prefix, raw, suffix = m.groups()
        # Hand-authored course HTML sometimes pads the attribute with spaces or
        # newlines; keeping them turns into %20 and a 404.
        raw = raw.strip()
        if raw.startswith(('data:', 'mailto:', 'javascript:', '#')) or not raw:
            return m.group(0)
        absolute = urllib.parse.urljoin(base_url, raw)
        if not absolute.startswith(('http://', 'https://')) or not should_localise(absolute):
            return prefix + absolute + suffix
        rel_asset = asset_local_path(absolute)
        assets[absolute] = rel_asset
        return prefix + web_rel(from_dir, rel_asset) + suffix

    return REF_RE.sub(repl, html)


def extract_body(html):
    m = re.search(r'<body[^>]*>(.*)</body>', html, re.S | re.I)
    body = m.group(1) if m else html
    m = re.search(r'<div[^>]*\bclass="[^"]*\bxblock\b[^"]*"[^>]*>(.*)</div>', body, re.S | re.I)
    return m.group(1) if m else body


# --------------------------------------------------------------------------
# Mirroring the real site design
# --------------------------------------------------------------------------

# The courseware UI is one webpack bundle on the MFE host; the written content is
# styled by the LMS theme. All of it is public static content, so the offline copy
# uses the real stylesheets rather than a hand-written imitation.
MFE_BASE = 'https://apps.madrasafree.com/learning/'
THEME_BASE = 'https://courses.madrasafree.com/static/madrasa-theme/'
THEME_CSS = [
    'https://cdn.rtlcss.com/bootstrap/v4.2.1/css/bootstrap.min.css',
    # Course pages link this from a CDN for their own icons; mirroring it here
    # (with its font files) keeps those icons alive offline.
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css',
    THEME_BASE + 'css/lms-style-vendor.68e48093f5dd.css',
    THEME_BASE + 'css/lms-main-v1-rtl.9979b3b61694.css',
    THEME_BASE + 'css/lms-style-course-vendor.730dda42e3c1.css',
    THEME_BASE + 'css/lms-course-rtl.1acea7dcdba9.css',
    THEME_BASE + 'css/custom.4dc9f07d3a45.css',
]
LOGO_URL = THEME_BASE + 'images/home/madrasa-beta.331f5f82bd55.png'

CSS_URL_RE = re.compile(r'''url\(\s*(['"]?)([^'")]+)\1\s*\)''')
CSS_IMPORT_RE = re.compile(r'''@import\s+(?:url\()?['"]([^'"]+)['"]\)?\s*;''')


def discover_mfe_css(client):
    """The app bundle name carries a content hash, so read it off the MFE index."""
    try:
        index = client.get_text(MFE_BASE)
    except Exception:
        return []
    return [urllib.parse.urljoin(MFE_BASE, href)
            for href in re.findall(r'href="(/learning/[^"]+\.css)"', index)]


def mirror_stylesheet(client, url, out_dir, done, errors):
    """Download a stylesheet and everything it references; return its local path."""
    if url in done:
        return done[url]
    rel = asset_local_path(url)
    done[url] = rel
    try:
        text = client.get_text(url)
    except Exception as e:
        errors.append(f"css | {url} | {e}")
        return rel
    if not text.strip():
        # A 200 with an empty body means the host served an SPA fallback, not the
        # stylesheet: worth shouting about rather than silently losing the design.
        errors.append(f"css | {url} | empty response, wrong host?")
        return rel
    css_dir = posixpath.dirname(rel)

    def fix_import(m):
        target = urllib.parse.urljoin(url, m.group(1))
        child = mirror_stylesheet(client, target, out_dir, done, errors)
        return f'@import url("{web_rel(css_dir, child)}");'

    def fix_url(m):
        quote, raw = m.group(1), m.group(2)
        if raw.startswith(('data:', '#')):
            return m.group(0)
        target = urllib.parse.urljoin(url, raw)
        if not target.startswith(('http://', 'https://')):
            return m.group(0)
        target_rel = asset_local_path(target)
        if target not in done:
            done[target] = target_rel
            try:
                client.download(target, os.path.join(out_dir, *target_rel.split('/')),
                                min_bytes=1)
            except Exception as e:
                errors.append(f"css-asset | {target} | {e}")
        return f'url({quote}{web_rel(css_dir, target_rel)}{quote})'

    text = CSS_IMPORT_RE.sub(fix_import, text)
    text = CSS_URL_RE.sub(fix_url, text)
    dest = os.path.join(out_dir, *rel.split('/'))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(text)
    return rel


def mirror_design(client, out_dir, errors):
    """Fetch the real stylesheets and logo. Returns (css_rel_paths, logo_rel)."""
    done = {}
    sheets = []
    # Theme first, MFE bundle last: the courseware chrome wins ties.
    for url in THEME_CSS + discover_mfe_css(client):
        sheets.append(mirror_stylesheet(client, url, out_dir, done, errors))
    logo_rel = asset_local_path(LOGO_URL)
    try:
        client.download(LOGO_URL, os.path.join(out_dir, *logo_rel.split('/')), min_bytes=1)
    except Exception as e:
        errors.append(f"logo | {LOGO_URL} | {e}")
        logo_rel = None
    log(f"Design mirrored: {len(sheets)} stylesheets, {len(done)} files")
    return sheets, logo_rel


# --------------------------------------------------------------------------
# Offline UI
# --------------------------------------------------------------------------

# Only the gaps the real stylesheets cannot cover offline: the content normally
# sits in an iframe sized by JS, and the video is a JS player.
EXTRA_CSS = """
.offline-unit-content{max-width:820px;margin:0 auto}
.offline-unit-content .xblock{margin-bottom:2.5rem}
.offline-unit-content img{max-width:100%;height:auto}
.offline-unit-content audio{width:100%;margin:.4rem 0}
.offline-unit-content iframe{max-width:100%}
.offline-video{width:100%;background:#000;display:block;border-radius:4px}
.offline-note{max-width:820px;margin:0 auto 1.5rem;padding:.7rem 1rem;border:1px dashed #d7d7d7;
              border-radius:4px;color:#6c757d;font-size:.9rem}
.sequence-navigation-tabs .btn-link{border-bottom:4px solid transparent;border-radius:0}
.sequence-navigation-tabs .btn-link.active{border-bottom-color:#0a3055;color:#0a3055}
.previous-btn.disabled,.next-btn.disabled{visibility:hidden;pointer-events:none}
.offline-unit-nav{display:flex;justify-content:space-between;gap:1rem;
                  border-top:1px solid #e1e4e8;margin-top:1rem;padding:1.1rem 0 .5rem}
.offline-unit-nav a{max-width:46%}
.offline-unit-nav a.disabled{visibility:hidden}
.offline-hint{color:#6c757d;font-size:.8rem;text-align:center;padding:0 0 2rem}
/* The LMS theme paints links in its own brand green; the courseware chrome uses
   #006daa, so restore it for the parts of the page that are chrome, not content. */
.offline-outline a,.offline-outline a:visited,.offline-unit-nav a,
.offline-unit-nav a:visited{color:#006daa}
.course-tabs-navigation .nav-link.active{color:#0a3055}
.offline-outline a{display:flex;align-items:center;gap:.55rem;padding:.55rem .2rem;
                   text-decoration:none}
.offline-outline li{border-top:1px solid #e1e4e8}
.offline-outline a:hover{background:#f8f9fa}
.offline-outline .kinds{margin-inline-start:auto;display:flex;gap:.35rem;color:#6c757d;
                        flex:0 0 auto}
details.offline-ch{border:1px solid #e1e4e8;border-radius:6px;margin-bottom:.6rem}
details.offline-ch>summary{padding:.8rem 1.1rem;background:#f8f9fa;cursor:pointer;
                           font-weight:700;color:#000}
.offline-sec{padding:0 1.1rem 1rem}
svg.ic{width:1.15em;height:1.15em;flex:0 0 auto;vertical-align:-.15em}
"""

ICON_SVG = {
    'video': '<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
             '<path d="M4 6h10a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z'
             'm14 4.5 4-2.5v8l-4-2.5z"/></svg>',
    'html': '<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
            '<path d="M6 2h8l6 6v14H6zm8 1.5V8h4.5zM8 12h8v1.6H8zm0 3.4h8V17H8z"/></svg>',
    'problem': '<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
               '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm.1 15.6a1.2 1.2 0 1 1 0-2.4'
               ' 1.2 1.2 0 0 1 0 2.4zm2.2-6.2c-.8.7-1.3 1.1-1.3 2H11c0-1.5.7-2.3 1.6-3'
               '.1.5-.5.8-.9.8-1.4 0-.7-.5-1.1-1.3-1.1s-1.4.5-1.4 1.4H9c0-1.9 1.3-3.1 3.2-3.1'
               '1.9 0 3.1 1.1 3.1 2.7 0 1-.5 1.7-1 2.2z"/></svg>',
    'discussion': '<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
                  '<path d="M4 4h16a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4V6a2 2 0 0 1 2-2z"/>'
                  '</svg>',
    'poll': '<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
            '<path d="M4 13h4v8H4zm6-9h4v17h-4zm6 5h4v12h-4z"/></svg>',
}


def icon(kind):
    return ICON_SVG.get(kind, ICON_SVG['html'])


NAV_JS = """
document.addEventListener('keydown', function (e) {
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  var t = e.target;
  if (t && (t.matches('input,textarea,select') || t.isContentEditable)) return;
  var go = e.key === 'ArrowLeft' ? 'nx' : e.key === 'ArrowRight' ? 'pv' : null;
  if (!go) return;
  var a = document.getElementById(go);
  if (a && a.getAttribute('href')) location.href = a.getAttribute('href');
});
"""


CHEVRON_PREV = ('<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
                '<path d="M8.6 4.6 16 12l-7.4 7.4L7.2 18l6-6-6-6z"/></svg>')
CHEVRON_NEXT = ('<svg class="ic" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
                '<path d="M15.4 4.6 8 12l7.4 7.4L16.8 18l-6-6 6-6z"/></svg>')

# Class names taken from the live courseware so the mirrored stylesheets apply.
BODY_CLASS = 'rtl view-in-course view-courseware courseware lang_he'


def page_shell(title, course_name, course_number, css_rels, logo_rel, here, body, extra_js=''):
    links = '\n'.join(f'<link rel="stylesheet" href="{web_rel(here, c)}">' for c in css_rels)
    links += f'\n<style>{EXTRA_CSS}</style>'
    home = web_rel(here, 'index.html')
    logo = ''
    if logo_rel:
        logo = (f'<a class="logo" href="{home}">'
                f'<img class="d-block" src="{web_rel(here, logo_rel)}" alt=""></a>')
    js = f'<script>{extra_js}</script>' if extra_js else ''
    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
{links}
</head>
<body class="{BODY_CLASS}">
<div id="root">
<header class="learning-header">
 <div class="container-xl py-2 d-flex align-items-center">
  {logo}
  <div class="flex-grow-1 course-title-lockup">
   <span class="d-block small m-0">{esc(course_number)}</span>
   <span class="d-block m-0 font-weight-bold course-title">{esc(course_name)}</span>
  </div>
 </div>
</header>
<main class="d-flex flex-column flex-grow-1">
 <div class="course-tabs-navigation mb-3">
  <div class="container-xl">
   <nav class="nav flex-nowrap nav-underline-tabs">
    <a class="nav-item flex-shrink-0 nav-link active" href="{home}">קורס</a>
   </nav>
  </div>
 </div>
{body}
</main>
</div>
{js}
</body>
</html>
"""


def render_unit_page(unit, out_dir, course_name, course_number, css_rels, logo_rel):
    here = unit['rel_dir']
    prev, nxt = unit['prev'], unit['next']
    prev_href = web_rel(here, prev['page']) if prev else ''
    next_href = web_rel(here, nxt['page']) if nxt else ''

    strip = []
    for sib in unit['siblings']:
        kinds = [c['type'] for c in sib['node']['children']]
        kind = 'video' if 'video' in kinds else (kinds[0] if kinds else 'html')
        classes = 'btn btn-link' + (' active' if sib is unit else '')
        href = '#' if sib is unit else web_rel(here, sib['page'])
        strip.append(f'<a class="{classes}" href="{href}" '
                     f'title="{esc(sib["node"]["name"])}">{icon(kind)}</a>')

    blocks = []
    for f in unit['files']:
        if f['type'] == 'video' and f.get('file'):
            blocks.append('<div class="xblock xblock-student_view"><video class="offline-video"'
                          f' controls preload="metadata" src="{urllib.parse.quote(f["file"])}">'
                          '</video></div>')
        elif f['type'] in CONTENT_TYPES and f.get('html'):
            blocks.append(f'<div class="xblock xblock-student_view">{f["html"]}</div>')
        elif f['type'] in NOT_SAVED:
            blocks.append(f'<div class="offline-note">{icon(f["type"])} '
                          f'{esc(f["title"])} — {NOT_SAVED[f["type"]]}</div>')
        elif f.get('note'):
            blocks.append(f'<div class="offline-note">{esc(f["title"])} — {esc(f["note"])}</div>')

    body = f"""<div class="container-xl">
 <div class="d-flex flex-column justify-content-center">
  <div class="sequence-container">
   <div class="sequence">
    <nav class="sequence-navigation mb-4">
     <a class="previous-btn btn btn-link{'' if prev else ' disabled'}" id="pv" href="{prev_href}"
        ><span class="pgn__icon btn-icon-before">{CHEVRON_PREV}</span>הקודם</a>
     <div><div class="sequence-navigation-tabs-container">
      <div class="sequence-navigation-tabs d-flex flex-grow-1">{''.join(strip)}</div>
     </div></div>
     <a class="next-btn btn btn-link{'' if nxt else ' disabled'}" id="nx" href="{next_href}"
        >הבא<span class="pgn__icon btn-icon-after">{CHEVRON_NEXT}</span></a>
    </nav>
    <div class="unit-container flex-grow-1">
     <div class="unit">
      <h1 class="mb-0 h3">{esc(unit['node']['name'])}</h1>
      <div class="offline-unit-content">
{''.join(blocks) or '<div class="offline-note">אין תוכן ביחידה זו</div>'}
      </div>
     </div>
    </div>
    <div class="offline-unit-nav container-xl">
     <a class="btn btn-link{'' if prev else ' disabled'}" href="{prev_href}"
        >{CHEVRON_PREV}{esc(prev['node']['name']) if prev else ''}</a>
     <a class="btn btn-link{'' if nxt else ' disabled'}" href="{next_href}"
        >{esc(nxt['node']['name']) if nxt else ''}{CHEVRON_NEXT}</a>
    </div>
    <div class="offline-hint">מקשי החיצים עוברים בין יחידות</div>
   </div>
  </div>
 </div>
</div>"""

    dest = os.path.join(out_dir, *here.split('/'), 'index.html')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(page_shell(f"{unit['node']['name']} | {course_name}", course_name,
                           course_number, css_rels, logo_rel, here, body, NAV_JS))


def render_outline_page(units, out_dir, course_name, course_number, css_rels, logo_rel):
    chapters = []
    for u in units:
        chapter = re.sub(r'^\d+ - ', '', u['trail'][0]) if u['trail'] else 'קורס'
        section = re.sub(r'^\d+ - ', '', u['trail'][1]) if len(u['trail']) > 1 else ''
        if not chapters or chapters[-1][0] != chapter:
            chapters.append((chapter, []))
        sections = chapters[-1][1]
        if not sections or sections[-1][0] != section:
            sections.append((section, []))
        sections[-1][1].append(u)

    parts = ['<ol class="list-unstyled offline-outline">']
    for ci, (chapter, sections) in enumerate(chapters):
        n_units = sum(len(us) for _, us in sections)
        n_vid = sum(1 for _, us in sections for u in us
                    for f in u['files'] if f['type'] == 'video' and f.get('file'))
        parts.append(f'<li><details class="offline-ch"{" open" if ci == 0 else ""}>'
                     f'<summary>{esc(chapter)}<span class="small text-muted">'
                     f' · {n_units} יחידות · {n_vid} סרטונים</span></summary>'
                     f'<div class="offline-sec">')
        for section, sec_units in sections:
            if section:
                parts.append(f'<h3 class="h5 text-muted mt-3 mb-1">{esc(section)}</h3>')
            parts.append('<ol class="list-unstyled">')
            for u in sec_units:
                seen = []
                for f in u['files']:
                    if f['type'] in NOT_SAVED or not (f.get('file') or f.get('html')):
                        continue
                    if f['type'] not in seen:
                        seen.append(f['type'])
                lead = icon('video' if 'video' in seen else (seen[0] if seen else 'html'))
                marks = ''.join(icon(t) for t in seen)
                parts.append(f'<li><a href="{urllib.parse.quote(u["page"])}">{lead}'
                             f'<span>{esc(u["node"]["name"])}</span>'
                             f'<span class="kinds">{marks}</span></a></li>')
            parts.append('</ol>')
        parts.append('</div></details></li>')
    parts.append('</ol>')

    total_vid = sum(1 for u in units for f in u['files']
                    if f['type'] == 'video' and f.get('file'))
    total_pages = sum(1 for u in units for f in u['files']
                      if f['type'] in CONTENT_TYPES and f.get('html'))
    body = (f'<div class="container-xl">\n'
            f'<div class="row w-100 mx-0 my-3"><div class="col-12 col-sm-auto p-0">'
            f'<div class="h2">{esc(course_name)}</div>'
            f'<p class="text-muted mb-0">עותק אופליין · {len(units)} יחידות · '
            f'{total_vid} סרטונים · {total_pages} עמודי תוכן</p></div></div>\n'
            f'<div class="row course-outline-tab"><div class="col col-12 col-md-8">\n'
            + '\n'.join(parts) + '\n</div></div>\n</div>')
    with open(os.path.join(out_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page_shell(course_name, course_name, course_number,
                           css_rels, logo_rel, '', body))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def download_course(client, tree, out_dir, course_number='', content_html=None,
                    want_videos=True, want_html=True, workers=4, dry_run=False):
    """content_html maps block_id -> {'html': ...} (bundle mode)."""
    units = plan_units(tree)
    counts = {}
    for u in units:
        for comp in u['node']['children']:
            counts[comp['type']] = counts.get(comp['type'], 0) + 1
    log(f"Course: {tree['name']}")
    log(f"Units: {len(units)} | " + ' | '.join(f"{k}: {v}" for k, v in sorted(counts.items())))

    if dry_run:
        for u in units:
            kinds = ', '.join(sorted({c['type'] for c in u['node']['children']})) or 'empty'
            log('  ' + ' / '.join(u['trail']) + f" / {u['node']['name']}  [{kinds}]")
        return

    os.makedirs(out_dir, exist_ok=True)
    if want_html:
        _remove_stale_pages(out_dir)
    errors = []
    assets = {}
    assets_lock = threading.Lock()

    def handle_unit(unit):
        unit_dir = os.path.join(out_dir, *unit['rel_dir'].split('/'))
        os.makedirs(unit_dir, exist_ok=True)
        for j, comp in enumerate(unit['node']['children'], 1):
            base = sanitize_filename(f"{j:02d} - {comp['name']}")
            record = {'type': comp['type'], 'title': comp['name']}
            try:
                if comp['type'] == 'video':
                    if want_videos:
                        record.update(_do_video(client, comp, unit_dir, base))
                    else:
                        record['note'] = 'skipped (--no-videos)'
                elif comp['type'] in CONTENT_TYPES:
                    if want_html:
                        record['html'] = _do_content(client, comp, unit['rel_dir'],
                                                     assets, assets_lock, content_html)
                    else:
                        record['note'] = 'skipped (--no-html)'
            except Exception as e:
                errors.append(f"{comp['type']} | {comp['name']} | {e}")
                record['note'] = f'failed: {e}'
            unit['files'].append(record)
        return unit

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for done, unit in enumerate(pool.map(handle_unit, units), 1):
            log(f"[{done}/{len(units)}] {unit['node']['name']}")

    if assets:
        log(f"\nDownloading {len(assets)} assets (images, audio, files)...")
        items = sorted(assets.items())

        def fetch_asset(pair):
            url, rel = pair
            try:
                client.download(url, os.path.join(out_dir, *rel.split('/')), min_bytes=1)
            except Exception as e:
                errors.append(f"asset | {url} | {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers * 2) as pool:
            for done, _ in enumerate(pool.map(fetch_asset, items), 1):
                if done % 25 == 0 or done == len(items):
                    log(f"  assets {done}/{len(items)}")

    log("\nMirroring the site design...")
    css_rels, logo_rel = mirror_design(client, out_dir, errors)

    log("Building offline pages...")
    for unit in units:
        render_unit_page(unit, out_dir, tree['name'], course_number, css_rels, logo_rel)
    render_outline_page(units, out_dir, tree['name'], course_number, css_rels, logo_rel)

    manifest = [{'title': u['node']['name'], 'path': u['trail'] + [u['node']['name']],
                 'page': u['page'],
                 'files': [{k: v for k, v in f.items() if k != 'html'} for f in u['files']]}
                for u in units]
    with open(os.path.join(out_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    videos = sum(1 for u in units for f in u['files'] if f['type'] == 'video' and f.get('file'))
    pages = sum(1 for u in units for f in u['files']
                if f['type'] in CONTENT_TYPES and f.get('html'))
    log(f"\nFinished in {(time.monotonic() - started) / 60:.1f} min -> {out_dir}")
    log(f"Units: {len(units)} | videos: {videos} | content blocks: {pages} | assets: {len(assets)}")
    log(f"Open {os.path.join(out_dir, 'index.html')}")
    if errors:
        log(f"\n{len(errors)} problems:")
        for e in errors[:60]:
            log('  ' + e)
        if len(errors) > 60:
            log(f"  ... and {len(errors) - 60} more")
    return units


def _remove_stale_pages(out_dir):
    """Pages are always regenerated; drop the ones from earlier layouts."""
    content_root = os.path.join(out_dir, 'content')
    if not os.path.isdir(content_root):
        return
    for dirpath, _, files in os.walk(content_root):
        for fn in files:
            if fn.endswith('.html'):
                os.remove(os.path.join(dirpath, fn))


def _do_video(client, comp, unit_dir, base):
    kind, url = pick_video_source(comp['student_view_data'])
    if kind == 'direct':
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or '.mp4'
        path = client.download(url, os.path.join(unit_dir, base + ext), min_bytes=10240)
    elif kind == 'ytdlp':
        path = run_ytdlp(url, os.path.join(unit_dir, base))
    else:
        raise RuntimeError('no downloadable video source')
    return {'file': os.path.basename(path), 'source': url, 'bytes': os.path.getsize(path)}


def _do_content(client, comp, rel_dir, assets, assets_lock, content_html):
    if content_html is not None:
        raw = content_html.get(comp['id'], {}).get('html')
        if raw is None:
            raise RuntimeError('block missing from bundle')
    else:
        raw = extract_body(client.get_text(
            comp.get('student_view_url') or f"/xblock/{comp['id']}"))
    local = {}
    body = rewrite_refs(raw, client.lms + '/', rel_dir, local)
    with assets_lock:
        assets.update(local)
    return body


# --------------------------------------------------------------------------

def cookie_header_from_file(path):
    jar = http.cookiejar.MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return '; '.join(f'{c.name}={c.value}' for c in jar)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('course_id', nargs='?', help='e.g. course-v1:madrasa+course2+2019_1')
    ap.add_argument('--bundle', help='JSON bundle extracted from a logged-in browser')
    ap.add_argument('--out', help='output directory')
    ap.add_argument('--lms', default=DEFAULT_LMS, help=f'LMS base URL (default {DEFAULT_LMS})')
    ap.add_argument('--sessionid', help='value of the sessionid cookie (live mode)')
    ap.add_argument('--cookie-header', help='full Cookie header string (live mode)')
    ap.add_argument('--cookies', help='path to a Netscape cookies.txt export (live mode)')
    ap.add_argument('--no-videos', action='store_true', help='skip videos')
    ap.add_argument('--no-html', action='store_true', help='skip written content')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--dry-run', action='store_true', help='list the structure and exit')
    args = ap.parse_args()

    cookie = (args.cookie_header
              or (cookie_header_from_file(args.cookies) if args.cookies else None)
              or (f'sessionid={args.sessionid}' if args.sessionid else None)
              or os.environ.get('EDX_COOKIE', ''))

    if args.bundle:
        with open(args.bundle, encoding='utf-8') as f:
            bundle = json.load(f)
        course_id = bundle.get('course_id') or args.course_id or 'course'
        tree = build_tree(bundle['blocks'])
        out = args.out or sanitize_filename(course_id.replace('course-v1:', ''))
        download_course(Client(args.lms, cookie), tree, out,
                        course_number=_course_number(course_id),
                        content_html=bundle.get('content') or {},
                        want_videos=not args.no_videos, want_html=not args.no_html,
                        workers=args.workers, dry_run=args.dry_run)
        return

    if not args.course_id:
        ap.error('provide a course_id, or --bundle')
    if not cookie:
        ap.error('live mode needs --sessionid, --cookie-header, --cookies or EDX_COOKIE')

    client = Client(args.lms, cookie)
    username = client.get_json('/api/user/v1/me')['username']
    log(f"Logged in as: {username}")
    meta = client.get_json(
        f'/api/course_home/course_metadata/{urllib.parse.quote(args.course_id)}')
    access = meta.get('course_access') or {}
    if not access.get('has_access'):
        raise SystemExit(f"No access as '{username}': {access.get('error_code')} — "
                         f"{access.get('user_message')}\nEnrol in the course first.")
    log(f"Course access OK (enrolled={meta.get('is_enrolled')})")

    data = fetch_blocks(client, args.course_id, username)
    out = args.out or sanitize_filename(args.course_id.replace('course-v1:', ''))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'course.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    download_course(client, build_tree(data), out,
                    course_number=_course_number(args.course_id),
                    want_videos=not args.no_videos, want_html=not args.no_html,
                    workers=args.workers, dry_run=args.dry_run)


def _course_number(course_id):
    """'course-v1:madrasa+course2+2019_1' -> 'madrasa course2'."""
    tail = course_id.split(':', 1)[-1]
    bits = tail.split('+')
    return ' '.join(bits[:2]) if len(bits) >= 2 else tail


if __name__ == '__main__':
    sys.exit(main())
