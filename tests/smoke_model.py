"""Optional real-model integration check; first run downloads verified weights.

Run from project root: python tests/smoke_model.py
Synthetic validation does not establish quality on real product photos.
"""
import sys
from pathlib import Path
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from engine import DEFAULTS, Segmenter, render


def main():
    root = Path(__file__).resolve().parents[1]
    model = Segmenter(root / '.models')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'sample.png'
        image = Image.new('RGB', (512, 512), 'white')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((170, 130, 340, 440), radius=25, fill=(28, 69, 121))
        draw.rectangle((210, 85, 300, 140), fill=(40, 40, 40))
        draw.rectangle((200, 230, 310, 330), fill=(240, 240, 230))
        draw.text((224, 275), 'PRODUCT', fill='black')
        image.save(path)
        original = path.read_bytes()
        result = render(path, {**DEFAULTS, 'output_size': [700, 700]}, model)
        assert result.mode == 'RGBA' and result.size == (700, 700)
        assert result.getpixel((0, 0))[3] == 0
        assert result.getpixel((350, 350))[3] > 240
        assert result.getchannel('A').getextrema()[1] > 240
        assert path.read_bytes() == original
        print('REAL_MODEL_SMOKE_OK: IS-Net + closed-form matting + RGBA + original unchanged')


if __name__ == '__main__':
    main()
