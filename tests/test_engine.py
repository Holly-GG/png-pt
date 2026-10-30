import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw
from engine import Batch, DEFAULTS, contact_shadow, load_config, render


class FakeSegmenter:
    def mask(self, rgb):
        # Deterministic oracle for flow tests; NOT model quality validation.
        data = np.asarray(rgb)
        return Image.fromarray(np.uint8(np.any(data < 180, axis=2)) * 255)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.target = self.root / 'output'
        self.config = {**DEFAULTS, 'output_size': [180, 180]}
        for index in range(4):
            im = Image.new('RGB', (100, 100), 'white')
            ImageDraw.Draw(im).rectangle((25, 15, 75, 80), fill=(30, 70, 120))
            im.save(self.source / f'{index}.png')

    def tearDown(self):
        self.temp.cleanup()

    def batch(self):
        return Batch(self.config, self.source, self.target, FakeSegmenter())

    def test_preview_confirmation_resume_and_originals(self):
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        batch = self.batch()
        with self.assertRaises(PermissionError):
            batch.run(True)
        previews = batch.preview()
        self.assertEqual(len(previews), 3)
        self.assertFalse(list(self.target.glob('*.png')))
        with self.assertRaises(PermissionError):
            batch.run(False)
        updates = []
        self.assertEqual(batch.run(True, lambda *args: updates.append(args)), (4, 0, 0))
        self.assertEqual(len(updates), 4)
        for out in self.target.glob('*.png'):
            with Image.open(out) as im:
                self.assertEqual(im.mode, 'RGBA')
                self.assertEqual(im.size, (180, 180))
                self.assertEqual(im.getpixel((0, 0))[3], 0)
                self.assertGreater(im.getpixel((90, 90))[3], 240)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir()})
        resumed = self.batch()
        self.assertEqual(resumed.preview(), [])
        self.assertTrue(all(resumed.completed(p) for p in resumed.files))

    def test_existing_output_and_changed_config_are_protected(self):
        batch = self.batch()
        batch.preview()
        batch.run(True)
        saved = (self.target / '0.png').read_bytes()
        self.config['brightness'] = 1.1
        with self.assertRaises(FileExistsError):
            self.batch().preview()
        self.assertEqual(saved, (self.target / '0.png').read_bytes())

    def test_same_stem_conflict(self):
        Image.new('RGB', (10, 10)).save(self.source / '0.jpg')
        with self.assertRaises(ValueError):
            self.batch()

    def test_failures_logged_and_later_images_continue(self):
        batch = self.batch()
        previewed = {p for p, _ in batch.preview()}
        other = next(p for p in batch.files if p not in previewed)
        other.write_bytes(b'broken image')
        self.assertEqual(batch.run(True), (3, 1, 0))
        self.assertIn(other.name, (self.target / 'error_log.txt').read_text())

    def test_changed_preview_source_requires_reapproval(self):
        batch = self.batch()
        path, _ = batch.preview()[0]
        path.write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            batch.run(True)
        self.assertFalse(list(self.target.glob('*.png')))

    def test_modified_preview_is_not_accepted(self):
        batch = self.batch()
        _, out = batch.preview()[0]
        out.write_bytes(b'altered preview')
        with self.assertRaises(RuntimeError):
            batch.run(True)
        self.assertFalse(list(self.target.glob('*.png')))

    def test_shadow_and_aspect(self):
        mask = Image.new('L', (200, 200))
        ImageDraw.Draw(mask).rectangle((60, 30, 140, 140), fill=255)
        shadow = np.asarray(contact_shadow(mask, self.config))
        self.assertGreater(shadow[141, 100], 0)
        self.assertEqual(shadow[0, 0], 0)
        self.assertEqual(shadow[60, 100], 0)
        config = {**self.config, 'shadow_strength': 0, 'output_size': [300, 150]}
        result = render(self.source / '0.png', config, FakeSegmenter())
        bbox = result.getchannel('A').getbbox()
        self.assertAlmostEqual((bbox[2] - bbox[0]) / (bbox[3] - bbox[1]), 51 / 66, delta=.04)

    def test_config_invalid_numbers_and_path_overlap(self):
        path = self.root / 'config.json'
        for change in [{'shadow_strength': 2}, {'brightness': float('nan')},
                       {'output_size': [1, 200]}, {'input_folder': 'input', 'output_folder': 'input/out'}]:
            path.write_text(json.dumps({**DEFAULTS, **change}), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
