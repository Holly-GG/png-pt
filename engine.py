"""Local product processing. Never writes to an input path."""
from __future__ import annotations
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback

import numpy as np
from PIL import Image, ImageCms, ImageEnhance, ImageFilter, ImageOps

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
VERSION = '1.0.0'
DEFAULTS = dict(input_folder='input', output_folder='output_png', output_size=None,
                shadow_strength=.18, shadow_blur=12, shadow_offset=[0, 2],
                edge_refinement=.5, brightness=1., contrast=1.02, sharpness=1.08)


def load_config(path):
    with open(path, encoding='utf-8-sig') as f:
        config = {**DEFAULTS, **json.load(f)}
    for key, low, high in [('shadow_strength', 0, 1), ('shadow_blur', 0, 100),
                           ('edge_refinement', 0, 1), ('brightness', .5, 1.5),
                           ('contrast', .5, 1.5), ('sharpness', 0, 2)]:
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} 必须在 {low} ~ {high} 之间')
    size = config['output_size']
    if size is not None and (not isinstance(size, list) or len(size) != 2 or
                            any(type(x) is not int or not 32 <= x <= 12000 for x in size)):
        raise ValueError('output_size 应为 null 或 [宽, 高]，范围 32~12000')
    offset = config['shadow_offset']
    if not isinstance(offset, list) or len(offset) != 2 or any(type(x) is not int or abs(x) > 100 for x in offset):
        raise ValueError('shadow_offset 应为 [-100~100, -100~100]')
    for key in ('input_folder', 'output_folder'):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f'{key} 不能为空')
    base = Path(path).resolve().parent
    source = (base / config['input_folder']).resolve()
    target = (base / config['output_folder']).resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('输入和输出目录必须分开，且不能相互包含')
    return config, source, target


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def png_bytes(image):
    data = io.BytesIO()
    image.save(data, format='PNG')
    return data.getvalue()


def exclusive_png(path, image):
    """No overwrite, even if another process creates the destination after scan."""
    data = png_bytes(image)
    created = False
    try:
        with open(path, 'xb') as f:
            created = True
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if created:
            Path(path).unlink(missing_ok=True)
        raise


def read_rgb(path):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        profile = im.info.get('icc_profile')
        rgba = im.convert('RGBA')
        alpha = rgba.getchannel('A')
        rgb = rgba.convert('RGB')
        if profile:
            rgb = ImageCms.profileToProfile(rgb, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                           ImageCms.createProfile('sRGB'), outputMode='RGB')
        white = Image.new('RGB', rgb.size, 'white')
        white.paste(rgb, mask=alpha)
        return white, alpha


class Segmenter:
    def __init__(self, model_dir):
        # rembg verifies its model checksum; do not disable verification.
        os.environ['U2NET_HOME'] = str(model_dir)
        os.environ.setdefault('OMP_NUM_THREADS', str(min(4, os.cpu_count() or 1)))
        from rembg import new_session
        self.session = new_session('isnet-general-use', providers=['CPUExecutionProvider'])

    def mask(self, rgb):
        # Model segmentation protects white product interiors, unlike white thresholding.
        mask = self.session.predict(rgb)[0]
        return mask.convert('L').resize(rgb.size, Image.Resampling.LANCZOS)



def matte_alpha(rgb, mask):
    """Closed-form matting: white interiors remain foreground, only boundaries unknown.

    Cap solver dimensions to bound memory for large batch photos. Original RGB is
    retained for final rendering; only alpha is upsampled.
    """
    from scipy.ndimage import binary_erosion, binary_dilation
    from pymatting import estimate_alpha_cf
    size = rgb.size
    small = rgb.copy()
    small.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    m = np.asarray(mask.resize(small.size, Image.Resampling.LANCZOS), dtype=np.float64) / 255
    foreground = binary_erosion(m > .94, iterations=3)
    background = ~binary_dilation(m > .04, iterations=3)
    if not foreground.any() or not background.any():
        return mask
    trimap = np.full(m.shape, .5)
    trimap[foreground] = 1
    trimap[background] = 0
    alpha = estimate_alpha_cf(np.asarray(small, dtype=np.float64) / 255, trimap)
    return Image.fromarray(np.uint8(np.clip(alpha, 0, 1) * 255)).resize(size, Image.Resampling.LANCZOS)


def refine(rgb, mask, amount):
    """Refine only transition pixels; estimate foreground under a white matte."""
    a = np.asarray(mask, dtype=np.float32) / 255
    if amount:
        smooth = np.asarray(mask.filter(ImageFilter.GaussianBlur(.35)), dtype=np.float32) / 255
        transition = (a > .02) & (a < .98)
        a[transition] = (1 - amount) * a[transition] + amount * smooth[transition]
    if np.count_nonzero(a > .5) < 16:
        raise ValueError('未识别到可靠的产品主体，请检查原图')
    c = np.asarray(rgb, dtype=np.float32) / 255
    # Conservative white decontamination, limited to semi-transparent edges.
    corrected = np.clip((c - (1 - a[..., None])) / np.maximum(a[..., None], .12), 0, 1)
    weight = (amount * ((a > .05) & (a < .95)))[..., None]
    c = c * (1 - weight) + corrected * weight
    a[a < .008] = 0
    return Image.fromarray(np.uint8(np.clip(c, 0, 1) * 255)), Image.fromarray(np.uint8(a * 255))


def contact_shadow(mask, config):
    """Use the mask's lowest contour, never a generic oval."""
    a = np.asarray(mask, dtype=np.float32) / 255
    solid = a > .5
    ys, xs = np.where(solid)
    shadow = np.zeros_like(a)
    if len(ys) == 0 or config['shadow_strength'] == 0:
        return Image.fromarray(np.uint8(shadow))
    bottom = int(ys.max())
    height = int(ys.max() - ys.min() + 1)
    depth = max(2, min(12, round(height * .015)))
    # Only columns touching the lowest support band contribute; handles don't cast feet.
    band = max(2, round(height * .025))
    dx, dy = config['shadow_offset']
    for x in np.unique(xs[ys >= bottom - band]):
        y = int(np.where(solid[:, x])[0][-1])
        for delta in range(-depth, depth + 1):
            xx, yy = int(x + dx), int(y + dy + delta)
            if 0 <= xx < a.shape[1] and 0 <= yy < a.shape[0]:
                shadow[yy, xx] = math.exp(-2 * (delta / depth) ** 2)
    blur = config['shadow_blur']
    soft = Image.fromarray(np.uint8(shadow * 255)).filter(ImageFilter.GaussianBlur(blur))
    # Keep a tight contact component so diffusion cannot make the object appear floating.
    tight = Image.fromarray(np.uint8(shadow * 255)).filter(ImageFilter.GaussianBlur(max(.5, blur / 5)))
    result = np.maximum(np.asarray(soft, dtype=float), np.asarray(tight, dtype=float) * .55)
    return Image.fromarray(np.uint8(result * config['shadow_strength']))


def render(path, config, segmenter):
    rgb, original_alpha = read_rgb(path)
    mask = segmenter.mask(rgb)
    if config['edge_refinement'] > 0 and isinstance(segmenter, Segmenter):
        mask = matte_alpha(rgb, mask)
    mask = Image.fromarray(np.minimum(np.asarray(mask), np.asarray(original_alpha)))
    rgb, mask = refine(rgb, mask, config['edge_refinement'])
    # Luminance-only adjustments preserve hue; soft highlight rolloff avoids clipping.
    lab = rgb.convert('YCbCr')
    y, cb, cr = lab.split()
    lum = np.asarray(y, dtype=np.float32) / 255
    adjusted = (lum - .5) * config['contrast'] + .5
    adjusted *= config['brightness']
    adjusted = np.where(adjusted > .9, .9 + .1 * np.tanh((adjusted - .9) / .1), adjusted)
    if config['brightness'] == 1 and config['contrast'] == 1:
        adjusted = lum
    if config['brightness'] != 1 or config['contrast'] != 1:
        rgb = Image.merge('YCbCr', (Image.fromarray(np.uint8(np.clip(adjusted, 0, 1) * 255)), cb, cr)).convert('RGB')
    if config['sharpness'] != 1:
        # Avoid sharpening the matte boundary against the discarded white background.
        enhanced = ImageEnhance.Sharpness(rgb).enhance(config['sharpness'])
        interior = mask.filter(ImageFilter.MinFilter(5))
        rgb = Image.composite(enhanced, rgb, interior)
    foreground = rgb.convert('RGBA')
    foreground.putalpha(mask)
    # Padding retains the shadow even when the product touches the source boundary.
    padding = math.ceil(3 * config['shadow_blur'] + max(map(abs, config['shadow_offset'])) + 16) if config['shadow_strength'] else 0
    canvas = Image.new('RGBA', (rgb.width + 2 * padding, rgb.height + 2 * padding))
    expanded_mask = Image.new('L', canvas.size)
    expanded_mask.paste(mask, (padding, padding))
    shadow = Image.new('RGBA', canvas.size)
    shadow.putalpha(contact_shadow(expanded_mask, config))
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(foreground, (padding, padding))
    size = config['output_size']
    if size:
        # Premultiplied-alpha resizing prevents dark fringes on transparency.
        fitted = ImageOps.contain(canvas.convert('RGBa'), tuple(size), Image.Resampling.LANCZOS).convert('RGBA')
        canvas = Image.new('RGBA', tuple(size))
        canvas.alpha_composite(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    return canvas


class Batch:
    def __init__(self, config, source, target, segmenter=None):
        self.config, self.source, self.target = config.copy(), Path(source), Path(target)
        if not self.source.is_dir():
            raise ValueError(f'输入文件夹不存在：{self.source}')
        if self.source.resolve() == self.target.resolve() or self.source.resolve() in self.target.resolve().parents or self.target.resolve() in self.source.resolve().parents:
            raise ValueError('输入和输出目录不能相互包含')
        self.files = sorted((p for p in self.source.iterdir() if p.suffix.lower() in EXTENSIONS and p.is_file()), key=lambda p: p.name.casefold())
        if not self.files:
            raise ValueError('输入目录中没有 JPG、JPEG、PNG 或 WEBP 图片')
        names = {}
        for p in self.files:
            key = p.stem.casefold()
            if key in names:
                raise ValueError(f'输出文件名冲突：{names[key]} 与 {p.name}。请在输入副本中改名后重试。')
            names[key] = p.name
        self.target.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.target / '.processed.json'
        if self.manifest_path.exists():
            with open(self.manifest_path, encoding='utf-8') as f:
                self.manifest = json.load(f)
        else:
            self.manifest = {}
        settings = {k: v for k, v in config.items() if k not in ('input_folder', 'output_folder')}
        self.signature = hashlib.sha256(json.dumps([VERSION, 'isnet-general-use', settings], sort_keys=True).encode()).hexdigest()
        self.segmenter = segmenter
        self.previews = {}
        self.preview_sources = {}
        self.preview_hashes = {}
        self.cancel = threading.Event()

    def model(self):
        if self.segmenter is None:
            self.segmenter = Segmenter(Path(__file__).resolve().parent / '.models')
        return self.segmenter

    def output(self, path):
        return self.target / (path.stem + '.png')

    def completed(self, path):
        entry = self.manifest.get(path.name)
        out = self.output(path)
        if not entry or not out.is_file() or entry.get('config') != self.signature:
            return False
        return entry.get('source') == digest(path) and entry.get('output') == digest(out)

    def log_error(self, path):
        with open(self.target / 'error_log.txt', 'a', encoding='utf-8') as f:
            f.write(f'\n[{time.strftime("%Y-%m-%d %H:%M:%S")}] {path}\n{traceback.format_exc()}\n')

    def preview(self, progress=lambda text: None):
        import random
        pending = [p for p in self.files if not self.completed(p)]
        if not pending:
            return []
        # Reject unmanaged or stale outputs instead of silently replacing user files.
        for path in pending:
            if self.output(path).exists():
                raise FileExistsError(f'已有输出未通过记录校验，拒绝覆盖：{self.output(path)}。请改用新的输出目录。')
        preview_root = self.target / 'previews'
        preview_root.mkdir(exist_ok=True)
        preview_dir = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d_%H%M%S_'), dir=preview_root))
        selected = random.sample(pending, min(3, len(pending)))
        results = []
        for i, path in enumerate(selected, 1):
            if self.cancel.is_set():
                raise InterruptedError('已取消')
            progress(f'生成预览 {i}/{len(selected)}：{path.name}（首次需下载模型）')
            try:
                before = digest(path)
                result = render(path, self.config, self.model())
                if digest(path) != before:
                    raise RuntimeError('处理期间原图发生变化，请重新预览')
                out = preview_dir / (path.stem + '.png')
                exclusive_png(out, result)
                self.previews[path] = out
                self.preview_hashes[path] = digest(out)
                self.preview_sources[path] = before
                results.append((path, out))
            except Exception:
                self.log_error(path)
                raise
        return results

    def run(self, approved, progress=lambda *args: None):
        if not approved or not self.previews:
            raise PermissionError('必须先生成预览并明确确认，才能批量处理')
        # Freeze the source set and previewed files between preview and confirmation.
        for path, value in self.preview_sources.items():
            if digest(path) != value:
                raise RuntimeError('预览后原图已改变，请重新生成预览')
            if digest(self.previews[path]) != self.preview_hashes[path]:
                raise RuntimeError('预览结果已改变，请重新生成预览')
        success = failed = skipped = 0
        for index, path in enumerate(self.files, 1):
            if self.cancel.is_set():
                break
            try:
                if self.completed(path):
                    skipped += 1
                else:
                    before = digest(path)
                    if path in self.previews:
                        with Image.open(self.previews[path]) as im:
                            result = im.convert('RGBA')
                    else:
                        result = render(path, self.config, self.model())
                    if digest(path) != before:
                        raise RuntimeError('处理期间原图发生变化')
                    exclusive_png(self.output(path), result)
                    self.manifest[path.name] = dict(source=before, config=self.signature, output=digest(self.output(path)))
                    atomic_json(self.manifest_path, self.manifest)
                    success += 1
            except Exception:
                failed += 1
                self.log_error(path)
            progress(index, len(self.files), success, failed, skipped, path.name)
        return success, failed, skipped
