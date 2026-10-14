#!/usr/bin/env python3
"""
RackMonitor - analog CRT status display for `gameserver`
Runs on a Libre Le Potato (S905X), writes BGRA frames straight to /dev/fb0.

Data source: Prometheus node_exporter on gameserver (:9100), including the
systemd collector so we can see which game server unit is active.

Layout
    center      : logo of the RUNNING game (bob), or offline icon (shake)
    zone1 (TL)  : machine uptime
    zone2 (TR)  : service uptime (of the running game unit)
    zone3 (BL)  : standby icons - the games that are NOT running (hidden if unreachable)
    zone4 (BR)  : total RAM usage %

Usage:
    python3 monitor.py                    # normal
    python3 monitor.py --calibrate        # CRT safe-area / zone overlay
    python3 monitor.py --fake             # cycle states, no network
    python3 monitor.py --preview out.png  # render one frame to PNG, no framebuffer
"""

import argparse
import configparser
import math
import os
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, 'config.ini')


# ---------------------------------------------------------------------------
# Prometheus text-format parser (stdlib only - no prometheus_client needed)
# ---------------------------------------------------------------------------

def _parse_labels(s):
    """Parse the inside of a {...} label set into a dict."""
    labels = {}
    i, n = 0, len(s)
    while i < n:
        eq = s.find('=', i)
        if eq < 0:
            break
        key = s[i:eq].strip()
        if eq + 1 >= n or s[eq + 1] != '"':
            break
        k = eq + 2
        buf = []
        while k < n:
            c = s[k]
            if c == '\\' and k + 1 < n:
                nxt = s[k + 1]
                buf.append({'n': '\n', 't': '\t', '\\': '\\', '"': '"'}.get(nxt, nxt))
                k += 2
                continue
            if c == '"':
                break
            buf.append(c)
            k += 1
        labels[key] = ''.join(buf)
        i = k + 1
        while i < n and s[i] in ', ':
            i += 1
    return labels


def parse_prom(text):
    """-> {metric_name: [(labels_dict, float_value), ...]}"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == '#':
            continue
        if '{' in line:
            open_b = line.index('{')
            close_b = line.rfind('}')
            if close_b < open_b:
                continue
            name = line[:open_b]
            labels = _parse_labels(line[open_b + 1:close_b])
            rest = line[close_b + 1:].split()
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, labels, rest = parts[0], {}, parts[1:]
        if not rest:
            continue
        try:
            val = float(rest[0])
        except ValueError:
            continue
        out.setdefault(name, []).append((labels, val))
    return out


def _first(metrics, name, default=None):
    rows = metrics.get(name)
    if not rows:
        return default
    return rows[0][1]


# ---------------------------------------------------------------------------
# Snapshot + poller
# ---------------------------------------------------------------------------

class Snapshot:
    """One scrape's worth of state. Uptimes are extrapolated locally between
    polls using a monotonic clock, so the display ticks every second instead of
    jumping every poll_interval."""

    def __init__(self, ok=False):
        self.ok = ok
        self.game = None              # 'ark' | 'rust' | 'sotf' | None
        self.machine_uptime = None    # seconds at capture time
        self.service_uptime = None
        self.ram_pct = None
        self.mono = time.monotonic()

    def _age(self):
        return time.monotonic() - self.mono

    def machine_uptime_now(self):
        return None if self.machine_uptime is None else self.machine_uptime + self._age()

    def service_uptime_now(self):
        return None if self.service_uptime is None else self.service_uptime + self._age()


class Poller(threading.Thread):
    def __init__(self, url, services, interval, timeout, fail_threshold):
        super().__init__(daemon=True)
        self.url = url
        self.services = services          # ordered {key: unit_name}
        self.interval = interval
        self.timeout = timeout
        self.fail_threshold = fail_threshold
        self._lock = threading.Lock()
        self._snap = Snapshot(ok=False)
        self._fails = 0
        self._stop = threading.Event()

    @property
    def snapshot(self):
        with self._lock:
            return self._snap

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = self._scrape()
                with self._lock:
                    self._snap = snap
                if self._fails:
                    print(f"[INFO] gameserver reachable again", flush=True)
                self._fails = 0
            except Exception as e:
                self._fails += 1
                print(f"[WARN] scrape failed ({self._fails}): {e}", flush=True)
                if self._fails >= self.fail_threshold:
                    with self._lock:
                        self._snap = Snapshot(ok=False)
            self._stop.wait(self.interval)

    def _scrape(self):
        req = urllib.request.Request(self.url, headers={'Accept': 'text/plain'})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            text = r.read().decode('utf-8', 'replace')
        m = parse_prom(text)

        snap = Snapshot(ok=True)

        # Use the REMOTE clock for both ends of the subtraction. The Le Potato
        # has no RTC, so never mix its own time into these calculations.
        now_remote = _first(m, 'node_time_seconds')
        boot = _first(m, 'node_boot_time_seconds')
        if now_remote is not None and boot is not None:
            snap.machine_uptime = max(0.0, now_remote - boot)

        total = _first(m, 'node_memory_MemTotal_bytes')
        avail = _first(m, 'node_memory_MemAvailable_bytes')
        if total:
            snap.ram_pct = max(0.0, min(100.0, (1.0 - (avail or 0.0) / total) * 100.0))

        active = {
            lbl.get('name')
            for lbl, val in m.get('node_systemd_unit_state', [])
            if lbl.get('state') == 'active' and val == 1.0
        }
        starts = {
            lbl.get('name'): val
            for lbl, val in m.get('node_systemd_unit_start_time_seconds', [])
        }

        for key, unit in self.services.items():   # dict order == priority
            if unit in active:
                snap.game = key
                st = starts.get(unit)
                if st and now_remote is not None and st > 0:
                    snap.service_uptime = max(0.0, now_remote - st)
                break

        return snap


class FakePoller:
    """--fake: rotate through every display state, no network."""
    SEQ = [('ark', True), ('rust', True), ('sotf', True), (None, True), (None, False)]

    def __init__(self, dwell=6.0):
        self.dwell = dwell
        self.t0 = time.monotonic()

    def start(self):
        pass

    def stop(self):
        pass

    @property
    def snapshot(self):
        i = int((time.monotonic() - self.t0) / self.dwell) % len(self.SEQ)
        game, ok = self.SEQ[i]
        s = Snapshot(ok=ok)
        if ok:
            s.machine_uptime = 3 * 86400 + 4 * 3600 + 812
            s.ram_pct = 34.0 + 40.0 * (i / len(self.SEQ))
            if game:
                s.game = game
                s.service_uptime = 7211 + i * 900
        return s


# ---------------------------------------------------------------------------
# Framebuffer
# ---------------------------------------------------------------------------

class Framebuffer:
    def __init__(self, path, width, height):
        self.path = path
        self.w, self.h = width, height
        self.stride = self._detect_stride()
        self._check_geometry()
        self.fd = os.open(path, os.O_RDWR)

    def _sysfs(self, attr):
        node = os.path.basename(self.path)
        try:
            with open(f'/sys/class/graphics/{node}/{attr}') as f:
                return f.read().strip()
        except OSError:
            return None

    def _detect_stride(self):
        s = self._sysfs('stride')
        try:
            v = int(s)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
        return self.w * 4

    def _check_geometry(self):
        vs = self._sysfs('virtual_size')
        bpp = self._sysfs('bits_per_pixel')
        if vs:
            try:
                fw, fh = (int(x) for x in vs.split(','))
                if (fw, fh) != (self.w, self.h):
                    print(f"[WARN] fb is {fw}x{fh} but config says {self.w}x{self.h}", flush=True)
            except ValueError:
                pass
        if bpp and bpp != '32':
            print(f"[WARN] fb is {bpp}bpp; this script writes 32bpp BGRA", flush=True)
        if self.stride != self.w * 4:
            print(f"[INFO] fb stride {self.stride} != {self.w * 4}, using per-line writes", flush=True)

    def write(self, canvas):
        r, g, b, a = canvas.split()
        data = Image.merge('RGBA', (b, g, r, a)).tobytes()
        rowlen = self.w * 4
        if self.stride == rowlen:
            os.pwrite(self.fd, data, 0)
        else:
            for y in range(self.h):
                os.pwrite(self.fd, data[y * rowlen:(y + 1) * rowlen], y * self.stride)

    def clear(self):
        row = b'\x00\x00\x00\xff' * self.w
        for y in range(self.h):
            os.pwrite(self.fd, row, y * self.stride)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Assets / drawing helpers
# ---------------------------------------------------------------------------

FALLBACK_COLORS = {
    'background': (16, 16, 24),
    'logo':       (70, 70, 90),
    'offline':    (110, 40, 40),
    'idle':       (60, 60, 60),
    'plate':      (0, 0, 0),
}


def hexcolor(s, default=(220, 220, 200)):
    s = (s or '').strip().lstrip('#')
    if len(s) == 6:
        try:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return default


def load_asset(path, size, strict, fallback_key, required=True):
    """Load + resize an RGBA asset. Returns None for optional missing assets."""
    if path and os.path.exists(path):
        try:
            return Image.open(path).convert('RGBA').resize(size, Image.LANCZOS)
        except Exception as e:
            print(f"[WARN] failed to load {path}: {e}", flush=True)
    if not required:
        return None
    if strict:
        raise FileNotFoundError(f"required asset missing: {path}")
    print(f"[WARN] missing asset, using placeholder: {path}", flush=True)
    color = FALLBACK_COLORS.get(fallback_key, (60, 60, 60))
    img = Image.new('RGBA', size, color + (255,))
    d = ImageDraw.Draw(img)
    d.line((0, 0, size[0], size[1]), fill=(255, 255, 255, 140), width=2)
    d.line((size[0], 0, 0, size[1]), fill=(255, 255, 255, 140), width=2)
    d.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=(255, 255, 255, 180), width=2)
    return img


def with_opacity(img, opacity):
    if img is None or opacity >= 255:
        return img
    out = img.copy()
    out.putalpha(img.split()[3].point(lambda p: (p * opacity) // 255))
    return out


def make_shadow(icon, dx, dy, opacity, blur=3):
    w, h = icon.size
    pad = abs(dx) + abs(dy) + blur * 3
    canvas = Image.new('RGBA', (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    shadow = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    shadow.paste(Image.new('RGBA', (w, h), (0, 0, 0, opacity)), mask=icon.split()[3])
    layer = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
    layer.paste(shadow, (pad + dx, pad + dy), shadow)
    layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    canvas.alpha_composite(layer)
    canvas.alpha_composite(icon, (pad, pad))
    return canvas, pad


class BobState:
    def __init__(self, amplitude, speed):
        self.amplitude, self.speed, self.phase = amplitude, speed, 0.0

    def update(self):
        self.phase = (self.phase + self.speed) % math.tau

    def offset(self):
        return int(math.sin(self.phase) * self.amplitude)


class ShakeState:
    def __init__(self, amplitude, duration, interval_min, interval_max):
        self.amplitude = amplitude
        self.duration = duration
        self.interval_min = interval_min
        self.interval_max = interval_max
        self._next = random.uniform(interval_min, interval_max)
        self._end = None
        self._last = time.monotonic()

    def update(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self._end is not None:
            if now >= self._end:
                self._end = None
                self._next = random.uniform(self.interval_min, self.interval_max)
            return
        self._next -= dt
        if self._next <= 0:
            self._end = now + self.duration

    def offset(self):
        if self._end is None:
            return 0
        return random.randint(-self.amplitude, self.amplitude)

    def trigger(self):
        self._end = time.monotonic() + self.duration


def fmt_uptime(sec):
    if sec is None:
        return '--'
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}:{m:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

class RackMonitor:

    def __init__(self, cfg_path, args):
        self.args = args
        self.cfg = configparser.ConfigParser()
        if not self.cfg.read(cfg_path):
            raise SystemExit(f"[ERROR] cannot read config: {cfg_path}")
        c = self.cfg

        self.W = c.getint('display', 'width')
        self.H = c.getint('display', 'height')
        self.fps = c.getint('display', 'fps', fallback=10)
        self.spf = 1.0 / self.fps
        self.strict = c.getboolean('display', 'strict_assets', fallback=False)
        self.offline_blank = c.getboolean('display', 'offline_blank_zones', fallback=False)

        self.services = {k: v for k, v in c.items('services')}

        # text
        font_path = c.get('text', 'font',
                          fallback='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
        self.f_value = self._font(font_path, c.getint('text', 'value_size', fallback=30))
        self.f_label = self._font(font_path, c.getint('text', 'label_size', fallback=15))
        self.col_value = hexcolor(c.get('text', 'value_color', fallback='#DCDCC8'))
        self.col_label = hexcolor(c.get('text', 'label_color', fallback='#909090'))
        self.col_stroke = hexcolor(c.get('text', 'outline_color', fallback='#0A0A0A'), (10, 10, 10))
        self.stroke_w = c.getint('text', 'outline_width', fallback=2)

        # icon motion
        self.icon_w = c.getint('icon', 'width')
        self.icon_h = c.getint('icon', 'height')
        self.icon_cx = c.getint('icon', 'center_x')
        self.icon_cy = c.getint('icon', 'center_y')
        self.sh_dx = c.getint('icon', 'shadow_offset_x', fallback=6)
        self.sh_dy = c.getint('icon', 'shadow_offset_y', fallback=8)
        self.sh_op = c.getint('icon', 'shadow_opacity', fallback=120)
        self.bob = BobState(c.getint('icon', 'bob_amplitude', fallback=10),
                            c.getfloat('icon', 'bob_speed', fallback=0.05))
        self.shake = ShakeState(c.getint('icon', 'shake_amplitude', fallback=8),
                                c.getfloat('icon', 'shake_duration', fallback=0.5),
                                c.getfloat('icon', 'shake_interval_min', fallback=4),
                                c.getfloat('icon', 'shake_interval_max', fallback=9))

        self.running = True
        self._load_assets()

        # data source
        if args.fake:
            self.poller = FakePoller()
        else:
            host = c.get('monitor', 'host')
            port = c.getint('monitor', 'port', fallback=9100)
            self.poller = Poller(
                url=f"http://{host}:{port}/metrics",
                services=self.services,
                interval=c.getfloat('monitor', 'poll_interval', fallback=5),
                timeout=c.getfloat('monitor', 'timeout', fallback=3),
                fail_threshold=c.getint('monitor', 'fail_threshold', fallback=2),
            )

        # framebuffer
        self.fb = None
        if not args.preview:
            self.fb = Framebuffer(c.get('display', 'framebuffer'), self.W, self.H)

        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_):
        self.running = False

    @staticmethod
    def _font(path, size):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            print(f"[WARN] font not found: {path} - falling back to PIL default", flush=True)
            return ImageFont.load_default()

    # -- assets -------------------------------------------------------------

    def _zone_box(self, z):
        c = self.cfg
        return (c.getint(z, 'x'), c.getint(z, 'y'),
                c.getint(z, 'width'), c.getint(z, 'height'))

    def _asset_path(self, key, standby=False):
        """Resolve a game's artwork. Explicit `logo_<key>` / `standby_<key>` keys in
        [assets] win; otherwise fall back to the pattern. Adding a game is then just
        dropping <key>.png in the assets dir - no [assets] edit needed."""
        c = self.cfg
        if standby:
            explicit = c.get('assets', f'standby_{key}', fallback='')
            if explicit:
                return explicit
            pat = c.get('assets', 'standby_pattern', fallback='')
            if pat and os.path.exists(pat.format(key=key)):
                return pat.format(key=key)
            # no dedicated standby art - reuse the main logo
        explicit = c.get('assets', f'logo_{key}', fallback='')
        if explicit:
            return explicit
        pat = c.get('assets', 'logo_pattern', fallback='')
        if pat:
            return pat.format(key=key)
        return ''

    def _load_assets(self):
        c, s = self.cfg, self.strict
        print("[INFO] loading assets...", flush=True)

        self.bg = load_asset(c.get('assets', 'background'), (self.W, self.H), s, 'background')

        isize = (self.icon_w, self.icon_h)
        self.logos = {
            k: load_asset(self._asset_path(k), isize, s, 'logo')
            for k in self.services
        }
        self.icon_offline = load_asset(c.get('assets', 'offline_icon'), isize, s, 'offline')
        self.icon_idle = load_asset(c.get('assets', 'idle_icon', fallback=''), isize, s, 'idle',
                                    required=False) or self.icon_offline

        # optional background plates behind each zone
        self.plates = {}
        for z in ('zone1', 'zone2', 'zone3', 'zone4'):
            x, y, w, h = self._zone_box(z)
            self.plates[z] = load_asset(c.get(z, 'plate', fallback=''), (w, h), s, 'plate',
                                        required=False)

        # standby icons (zone3) - reuse the logo PNGs, scaled + dimmed.
        # The grid is solved for the WORST case (every game in standby, i.e. the
        # idle state) so icons never resize when a server starts or stops.
        self.standby_opacity = c.getint('zone3', 'icon_opacity', fallback=110)
        self.sb_layout = self._solve_standby_grid(len(self.services))
        sb = self.sb_layout['size']
        self.standby = {
            k: with_opacity(
                load_asset(self._asset_path(k, standby=True), (sb, sb), s, 'logo'),
                self.standby_opacity)
            for k in self.services
        }
        print(f"[INFO] assets loaded. standby grid: {len(self.services)} slots, "
              f"{self.sb_layout['cols']}x{self.sb_layout['rows']} @ {sb}px"
              + (f", {self.sb_layout['overflow']} overflow" if self.sb_layout['overflow'] else ""),
              flush=True)

    def _solve_standby_grid(self, n):
        """Pick the rows/cols/icon-size that packs n icons into zone3 as large as
        possible. Returns the largest arrangement that still clears icon_size_min;
        if nothing fits, drops icons and reports an overflow count for a '+N' badge."""
        c = self.cfg
        _, _, w, h = self._zone_box('zone3')
        gap = c.getint('zone3', 'icon_gap', fallback=12)
        pad = c.getint('zone3', 'icon_pad', fallback=8)
        head = c.getint('zone3', 'label_y', fallback=16) + c.getint('text', 'label_size',
                                                                    fallback=15)
        smax = c.getint('zone3', 'icon_size_max', fallback=64)
        smin = c.getint('zone3', 'icon_size_min', fallback=26)

        inner_w = w - 2 * pad
        inner_h = h - head - pad

        def best(count):
            top = None
            for cols in range(1, count + 1):
                rows = math.ceil(count / cols)
                sw = (inner_w - (cols - 1) * gap) // cols
                sh = (inner_h - (rows - 1) * gap) // rows
                size = min(sw, sh, smax)
                if size > 0 and (top is None or size > top['size']):
                    top = {'size': size, 'cols': cols, 'rows': rows,
                           'gap': gap, 'pad': pad, 'head': head, 'shown': count,
                           'overflow': 0}
            return top

        shown = n
        while shown > 1:
            lay = best(shown)
            if lay and lay['size'] >= smin:
                lay['overflow'] = n - shown
                return lay
            shown -= 1          # too cramped - drop one and badge the rest
        lay = best(1) or {'size': smin, 'cols': 1, 'rows': 1, 'gap': gap,
                          'pad': pad, 'head': head, 'shown': 1, 'overflow': 0}
        lay['overflow'] = n - 1
        return lay

    # -- zone renderers -----------------------------------------------------

    def _text_tile(self, zone, label, value):
        x, y, w, h = self._zone_box(zone)
        tile = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        plate = self.plates[zone]
        if plate:
            tile.alpha_composite(plate)
        d = ImageDraw.Draw(tile)
        ly = self.cfg.getint(zone, 'label_y', fallback=int(h * 0.26))
        vy = self.cfg.getint(zone, 'value_y', fallback=int(h * 0.66))
        d.text((w // 2, ly), label, font=self.f_label, fill=self.col_label,
               anchor='mm', stroke_width=self.stroke_w, stroke_fill=self.col_stroke)
        d.text((w // 2, vy), value, font=self.f_value, fill=self.col_value,
               anchor='mm', stroke_width=self.stroke_w, stroke_fill=self.col_stroke)
        return tile, (x, y)

    def _standby_tile(self, running_key):
        zone = 'zone3'
        x, y, w, h = self._zone_box(zone)
        tile = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        plate = self.plates[zone]
        if plate:
            tile.alpha_composite(plate)

        keys = [k for k in self.services if k != running_key]
        if not keys:
            return tile, (x, y)

        d = ImageDraw.Draw(tile)
        d.text((w // 2, self.cfg.getint(zone, 'label_y', fallback=16)),
               self.cfg.get(zone, 'label', fallback='STANDBY'),
               font=self.f_label, fill=self.col_label, anchor='mm',
               stroke_width=self.stroke_w, stroke_fill=self.col_stroke)

        lay = self.sb_layout
        sb, gap, cols = lay['size'], lay['gap'], lay['cols']
        cap = lay['shown']
        hidden = max(0, len(keys) - cap)
        draw_keys = keys[:cap]

        rows = math.ceil(len(draw_keys) / cols)
        block_h = rows * sb + (rows - 1) * gap
        top = lay['head'] + max(0, (h - lay['head'] - lay['pad'] - block_h) // 2)

        for i, k in enumerate(draw_keys):
            r, col = divmod(i, cols)
            in_row = min(cols, len(draw_keys) - r * cols)
            row_w = in_row * sb + (in_row - 1) * gap
            ix = (w - row_w) // 2 + col * (sb + gap)
            iy = top + r * (sb + gap)
            img = self.standby[k]
            if img:
                tile.alpha_composite(img, (ix, iy))

        if hidden:
            d.text((w - lay['pad'], h - lay['pad']), f"+{hidden}", font=self.f_label,
                   fill=self.col_label, anchor='rs',
                   stroke_width=self.stroke_w, stroke_fill=self.col_stroke)
        return tile, (x, y)

    def _ram_tile(self, pct):
        zone = 'zone4'
        tile, pos = self._text_tile(
            zone,
            self.cfg.get(zone, 'label', fallback='RAM'),
            '--' if pct is None else f"{pct:.0f}%")
        if self.cfg.getboolean(zone, 'bar', fallback=True) and pct is not None:
            x, y, w, h = self._zone_box(zone)
            d = ImageDraw.Draw(tile)
            bw = self.cfg.getint(zone, 'bar_width', fallback=int(w * 0.72))
            bh = self.cfg.getint(zone, 'bar_height', fallback=10)
            by = self.cfg.getint(zone, 'bar_y', fallback=h - bh - 12)
            bx = (w - bw) // 2
            warn = self.cfg.getfloat(zone, 'bar_warn', fallback=75)
            crit = self.cfg.getfloat(zone, 'bar_crit', fallback=90)
            fill = hexcolor(self.cfg.get(zone, 'bar_color_ok', fallback='#54B054'), (84, 176, 84))
            if pct >= crit:
                fill = hexcolor(self.cfg.get(zone, 'bar_color_crit', fallback='#C04040'), (192, 64, 64))
            elif pct >= warn:
                fill = hexcolor(self.cfg.get(zone, 'bar_color_warn', fallback='#C8A030'), (200, 160, 48))
            d.rectangle((bx, by, bx + bw, by + bh), fill=(0, 0, 0, 140),
                        outline=self.col_label + (255,), width=1)
            fw = int(bw * pct / 100.0)
            if fw > 1:
                d.rectangle((bx + 1, by + 1, bx + fw - 1, by + bh - 1), fill=fill + (255,))
        return tile, pos

    # -- frame --------------------------------------------------------------

    def _build_frame(self, snap):
        canvas = self.bg.copy()

        if snap.ok:
            canvas.alpha_composite(*self._text_tile(
                'zone1', self.cfg.get('zone1', 'label', fallback='MACHINE UP'),
                fmt_uptime(snap.machine_uptime_now())))
            canvas.alpha_composite(*self._text_tile(
                'zone2', self.cfg.get('zone2', 'label', fallback='SERVICE UP'),
                fmt_uptime(snap.service_uptime_now()) if snap.game else 'IDLE'))
            canvas.alpha_composite(*self._standby_tile(snap.game))
            canvas.alpha_composite(*self._ram_tile(snap.ram_pct))
        else:
            # zone3 (standby) is hidden entirely when gameserver is unreachable
            if not self.offline_blank:
                canvas.alpha_composite(*self._text_tile(
                    'zone1', self.cfg.get('zone1', 'label', fallback='MACHINE UP'), '--'))
                canvas.alpha_composite(*self._text_tile(
                    'zone2', self.cfg.get('zone2', 'label', fallback='SERVICE UP'), '--'))
                canvas.alpha_composite(*self._ram_tile(None))

        # center icon
        if not snap.ok:
            self.shake.update()
            icon, dx, dy = self.icon_offline, self.shake.offset(), 0
        else:
            self.bob.update()
            icon = self.logos.get(snap.game) if snap.game else self.icon_idle
            dx, dy = 0, self.bob.offset()

        shadowed, pad = make_shadow(icon, self.sh_dx, self.sh_dy, self.sh_op)
        ix = self.icon_cx - self.icon_w // 2 + dx - pad
        iy = self.icon_cy - self.icon_h // 2 + dy - pad
        canvas.alpha_composite(shadowed, (ix, iy))

        return canvas

    def _build_calibration(self):
        canvas = Image.new('RGBA', (self.W, self.H), (0, 0, 0, 255))
        d = ImageDraw.Draw(canvas)
        for gx in range(0, self.W + 1, 40):
            d.line((gx, 0, gx, self.H), fill=(40, 40, 40, 255))
        for gy in range(0, self.H + 1, 36):
            d.line((0, gy, self.W, gy), fill=(40, 40, 40, 255))
        # 10% (title) and 5% (action) safe areas
        for pct, col, name in ((0.10, (200, 60, 60), 'TITLE SAFE 10%'),
                               (0.05, (60, 160, 200), 'ACTION SAFE 5%')):
            mx, my = int(self.W * pct), int(self.H * pct)
            d.rectangle((mx, my, self.W - mx, self.H - my), outline=col + (255,), width=2)
            d.text((mx + 6, my + 4), name, font=self.f_label, fill=col + (255,))
        for z in ('zone1', 'zone2', 'zone3', 'zone4'):
            x, y, w, h = self._zone_box(z)
            d.rectangle((x, y, x + w, y + h), outline=(230, 230, 120, 255), width=2)
            d.text((x + 4, y + 4), z, font=self.f_label, fill=(230, 230, 120, 255))
        d.rectangle((self.icon_cx - self.icon_w // 2, self.icon_cy - self.icon_h // 2,
                     self.icon_cx + self.icon_w // 2, self.icon_cy + self.icon_h // 2),
                    outline=(120, 230, 120, 255), width=2)
        d.text((self.W // 2, self.H // 2), 'ICON', font=self.f_label,
               fill=(120, 230, 120, 255), anchor='mm')
        return canvas

    # -- main loop ----------------------------------------------------------

    def run(self):
        if self.args.preview:
            snap = self.poller.snapshot if self.args.fake else Snapshot(ok=False)
            frame = self._build_calibration() if self.args.calibrate else self._build_frame(snap)
            frame.convert('RGB').save(self.args.preview)
            print(f"[INFO] wrote preview -> {self.args.preview}", flush=True)
            return

        print(f"[INFO] {self.W}x{self.H} @ {self.fps}fps -> {self.cfg.get('display', 'framebuffer')}",
              flush=True)
        self.poller.start()

        try:
            while self.running:
                t0 = time.monotonic()
                if self.args.calibrate:
                    frame = self._build_calibration()
                else:
                    frame = self._build_frame(self.poller.snapshot)
                self.fb.write(frame)
                sleep = self.spf - (time.monotonic() - t0)
                if sleep > 0:
                    time.sleep(sleep)
        finally:
            self.poller.stop()
            print("\n[INFO] clearing framebuffer, exiting.", flush=True)
            self.fb.clear()
            self.fb.close()


# ---------------------------------------------------------------------------

def doctor(cfg_path):
    """Cross-check [services] against the artwork on disk and the units gameserver
    is actually exporting. This is the 'I added a game and it won't show up' tool."""
    args = argparse.Namespace(config=cfg_path, calibrate=False, fake=False, preview='/dev/null')
    rm = RackMonitor(cfg_path, args)
    c = rm.cfg
    ok = True

    print("\n=== services ===")
    for k, unit in rm.services.items():
        print(f"  {k:<10} -> {unit}")
    if not rm.services:
        print("  [FAIL] [services] is empty")
        ok = False

    print("\n=== artwork ===")
    for k in rm.services:
        p = rm._asset_path(k)
        mark = "OK  " if p and os.path.exists(p) else "FAIL"
        ok &= mark == "OK  "
        print(f"  [{mark}] {k:<10} {p or '(no logo_%s and no logo_pattern)' % k}")
    for name, key in (('background', 'background'), ('offline icon', 'offline_icon'),
                      ('idle icon', 'idle_icon')):
        p = c.get('assets', key, fallback='')
        exists = bool(p) and os.path.exists(p)
        print(f"  [{'OK  ' if exists else 'WARN'}] {name:<10} {p or '(unset)'}")

    print("\n=== zone3 standby layout ===")
    lay = rm.sb_layout
    print(f"  {len(rm.services)} games -> {lay['cols']}x{lay['rows']} grid @ {lay['size']}px")
    if lay['overflow']:
        print(f"  [WARN] {lay['overflow']} icon(s) won't fit and will show as '+N'.")
        print(f"         Widen zone3 or lower icon_size_min, then re-run --calibrate.")

    print("\n=== gameserver ===")
    host = c.get('monitor', 'host')
    port = c.getint('monitor', 'port', fallback=9100)
    url = f"http://{host}:{port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=c.getfloat('monitor', 'timeout', fallback=3)) as r:
            m = parse_prom(r.read().decode('utf-8', 'replace'))
        print(f"  [OK  ] scraped {url}")
    except Exception as e:
        print(f"  [FAIL] {url}: {e}")
        return 1

    for metric in ('node_time_seconds', 'node_boot_time_seconds',
                   'node_memory_MemTotal_bytes', 'node_memory_MemAvailable_bytes',
                   'node_systemd_unit_state'):
        present = metric in m
        ok &= present
        print(f"  [{'OK  ' if present else 'FAIL'}] {metric}")
    if 'node_systemd_unit_start_time_seconds' not in m:
        ok = False
        print("  [FAIL] node_systemd_unit_start_time_seconds")
        print("         -> add --collector.systemd.enable-start-time-metrics to node_exporter")
    else:
        print("  [OK  ] node_systemd_unit_start_time_seconds")

    exported = {lbl.get('name') for lbl, _ in m.get('node_systemd_unit_state', [])}
    active = {lbl.get('name') for lbl, v in m.get('node_systemd_unit_state', [])
              if lbl.get('state') == 'active' and v == 1.0}
    configured = set(rm.services.values())

    print("\n=== unit cross-check ===")
    print(f"  active now : {', '.join(sorted(active)) or '(none - display shows idle icon)'}")
    if len(active & configured) > 1:
        print(f"  [WARN] more than one configured unit is active - check your Conflicts= lines")
    for unit in sorted(exported - configured):
        ok = False
        print(f"  [FAIL] {unit} is exported by gameserver but missing from [services].")
        print(f"         -> add it to config.ini and drop its PNG in the assets dir")
    for unit in sorted(configured - exported):
        print(f"  [note] {unit} not in metrics (normal if it's never been started - "
              f"systemd loads units lazily, and standby is derived from config, not metrics)")

    print("\n" + ("ALL GOOD" if ok else "PROBLEMS FOUND - see FAIL lines above"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="RackMonitor CRT display")
    ap.add_argument('-c', '--config', default=DEFAULT_CONFIG)
    ap.add_argument('--doctor', action='store_true',
                    help='validate config/assets against what gameserver is exporting')
    ap.add_argument('--calibrate', action='store_true',
                    help='draw safe-area + zone overlay instead of live data')
    ap.add_argument('--fake', action='store_true',
                    help='cycle through all states without touching the network')
    ap.add_argument('--preview', metavar='PNG',
                    help='render a single frame to a PNG instead of the framebuffer')
    args = ap.parse_args()
    if args.doctor:
        sys.exit(doctor(args.config))
    RackMonitor(args.config, args).run()


if __name__ == '__main__':
    main()
