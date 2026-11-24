"""Protect recorded source data when producing a separate affine audit."""
from pathlib import Path
import tempfile
import unittest

from oscilloscope.audit_affine_frequency_tracking import (
    audit, checked_output_directory, csv_write,
)


class AffineFrequencyAuditOutputTests(unittest.TestCase):
    def test_empty_optional_csv_replaces_previous_record_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'sf_request_source_audit.csv'
            path.write_text('frequency_hz\n99\n', encoding='utf-8')
            csv_write(path, [])
            self.assertEqual(path.read_text(encoding='utf-8'), '')

    def test_rejects_source_directory_and_descendants(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)/'run'
            run.mkdir()
            for output in (run, run/'analysis', run/'new'/'diagnostics'):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(ValueError, 'outside the original'):
                        checked_output_directory(run, output)

    def test_rejects_symlink_alias_into_recorded_source(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)/'run'
            run.mkdir()
            alias = Path(d)/'output_alias'
            alias.symlink_to(run, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'outside the original'):
                checked_output_directory(run, alias/'new_diagnostics')

    def test_allows_a_sibling_directory_with_shared_name_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)/'run'
            output = Path(d)/'run_audit'
            self.assertEqual(checked_output_directory(run, output),output.resolve())
            self.assertFalse(output.exists())

    def test_audit_refuses_unsafe_target_before_any_source_reads(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)/'run_without_metadata'
            run.mkdir()
            with self.assertRaisesRegex(ValueError, 'outside the original'):
                audit(run, run/'analysis')
            self.assertEqual(list(run.iterdir()),[])


if __name__ == '__main__':
    unittest.main()
