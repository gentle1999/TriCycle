from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tricycle_core.io import file_sha256
from tricycle_core.training_protocol import verify_asset_files
from tricycle_sar import environment
from tricycle_sar.backends.environments.engine import (
    pypi_fallback_environment,
    validate_base_versions,
)
from tricycle_sar.environment.validated import plan_backend


class ModelEnvironmentTests(unittest.TestCase):
    def test_training_bundle_hashing_works_without_hashlib_file_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'payload.bin'
            payload = b'ground-truth bundle' * 90000
            path.write_bytes(payload)
            with patch('hashlib.file_digest', create=True, side_effect=AttributeError):
                self.assertEqual(file_sha256(path), hashlib.sha256(payload).hexdigest())

    def test_all_plans_are_offline_and_do_not_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'not-created'
            with patch('subprocess.run', side_effect=AssertionError('plan invoked subprocess')):
                for model in ['chemprop_cgr', 't5chem', 'rxngraphormer', 'reactiont5', 'rxnresid', 'uam', 'molclr']:
                    result = plan_backend(model, root)
                    self.assertEqual(len(result['revision']), 40)
                    self.assertFalse(result['training_started'])
            self.assertFalse(root.exists())

    def test_unregistered_environments_are_rejected(self) -> None:
        for model in ['graphrxn', 'reamvp']:
            with self.assertRaisesRegex(ValueError, 'Unknown SAR model'):
                plan_backend(model, Path('/tmp/environment-plan-only'))

    def test_assets_reject_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{'path': 'checkpoint', 'sha256': hashlib.sha256(b'actual').hexdigest()}]
            self.assertEqual(verify_asset_files(root, entries), [str(root / 'checkpoint')])
            (root / 'checkpoint').write_bytes(b'actual')
            self.assertEqual(verify_asset_files(root, entries), [])
            (root / 'checkpoint').write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                verify_asset_files(root, entries)

    def test_base_cuda_version_and_distribution_version_are_distinct(self) -> None:
        self.assertEqual(validate_base_versions(['2.12.1+cu130', '3.12', '2.12.1'],
                                               '2.12.1+cu130', '3.12'), '2.12.1')
        with self.assertRaises(RuntimeError):
            validate_base_versions(['2.12.1+cpu', '3.12', '2.12.1'], '2.12.1+cu130', '3.12')

    def test_pypi_fallback_clears_inherited_index_overrides(self) -> None:
        source = {
            'PIP_INDEX_URL': 'https://pypi.tuna.tsinghua.edu.cn/simple',
            'PIP_EXTRA_INDEX_URL': 'https://blocked.example/simple',
            'PIP_FIND_LINKS': 'https://blocked.example/wheels',
            'PIP_NO_INDEX': '1',
            'PIP_CONFIG_FILE': '/tmp/custom-pip.conf',
            'PATH': '/usr/bin',
        }

        result = pypi_fallback_environment(source)

        self.assertEqual(result['PIP_INDEX_URL'], 'https://pypi.org/simple')
        self.assertEqual(result['PIP_CONFIG_FILE'], '/dev/null')
        self.assertNotIn('PIP_EXTRA_INDEX_URL', result)
        self.assertNotIn('PIP_FIND_LINKS', result)
        self.assertNotIn('PIP_NO_INDEX', result)
        self.assertEqual(result['PATH'], '/usr/bin')
        self.assertEqual(source['PIP_INDEX_URL'], 'https://pypi.tuna.tsinghua.edu.cn/simple')

    def test_probe_parses_under_supported_python(self) -> None:
        ast.parse(Path(environment.__file__).with_name("verify.py").read_text(), feature_version=(3, 11))


if __name__ == '__main__':
    unittest.main()
