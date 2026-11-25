"""Hardware-free checks for separate evidence/results/Git report publication."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from experiment_analysis.paths import analysis_root, results_dir, use_results_root, sha256
from experiment_analysis.migrate_layout import migrate, inventory, verify_migration
from experiment_analysis.register_recording import register
from experiment_analysis.finalize_layout import finalize
from experiment_analysis.publish_reports import publish, _safe_relative


class SeparateRecordingAndReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.raw = self.home/'data'/'recordings'/'sample_run'
        self.raw.mkdir(parents=True)
        (self.raw/'waveforms').mkdir()
        ch1 = self.raw/'waveforms'/'ch1.csv'
        ch1.write_bytes(b'time,v\n0.0,0\n')
        (self.raw/'metadata.json').write_text(json.dumps({
            'run_id':'sample_run', 'status':'complete',
            'records':[{'index':1,'channels':{
                'CH1':{'path':'waveforms/ch1.csv'}}}]
        })+'\n')
        a = self.raw/'analysis'
        a.mkdir()
        (a/'figures').mkdir()
        (a/'figures'/'plot.png').write_bytes(b'fake-png-fixture')
        (a/'final_experiment_report.md').write_text(
            '# Final report\n\n![Figure 1](figures/plot.png)\n\n'
            '[Detailed evidence](command_timing/report.md)\n', encoding='utf-8')
        (a/'extended_analysis_technical_report.md').write_text(
            '# Technical report\n\n![Figure X](figures/plot.png)\n', encoding='utf-8')
        (a/'pulse_review').mkdir()
        (a/'pulse_review'/'review_approval.json').write_text(
            json.dumps({'human_signed_off': True, 'record_index':1,
                        'source_ch1_sha256':sha256(ch1)}) + '\n')
        self.resultroot = self.home/'results'

    def test_dry_run_does_not_create_results(self):
        original = inventory(self.raw/'analysis')
        info = migrate(self.raw, results_root=self.resultroot)
        self.assertEqual(info['status'], 'DRY_RUN')
        self.assertEqual(info['analysis_file_count'], len(original))
        self.assertFalse(self.resultroot.exists())
        self.assertEqual(inventory(self.raw/'analysis'), original)

    def test_copy_verification_and_binding_keep_raw_unchanged(self):
        raw_hash = sha256(self.raw/'waveforms/ch1.csv')
        source = inventory(self.raw/'analysis')
        result = migrate(self.raw, results_root=self.resultroot, apply=True)
        target = self.resultroot/'sample_run'
        self.assertEqual(result['status'], 'VERIFIED_COPY')
        self.assertEqual(inventory(target/'analysis'), source)
        self.assertEqual(verify_migration(self.raw, results_root=self.resultroot)['status'], 'VERIFIED')
        self.assertEqual(sha256(self.raw/'waveforms/ch1.csv'), raw_hash)
        self.assertTrue((self.raw/'analysis/pulse_review/review_approval.json').exists())
        (target/'analysis/pulse_review/review_decisions.csv').write_text('tamper')
        # A newly added CSV does not rewrite historical hashes, but a signed
        # source file must never change unnoticed.
        (target/'analysis/pulse_review/review_approval.json').write_text('tamper')
        with self.assertRaisesRegex(ValueError, 'hash mismatch|size mismatch'):
            verify_migration(self.raw, results_root=self.resultroot)
        (target/'analysis/pulse_review/review_approval.json').write_bytes(
            (self.raw/'analysis/pulse_review/review_approval.json').read_bytes())
        with use_results_root(self.resultroot):
            self.assertEqual(analysis_root(self.raw), target/'analysis')
        manifest = json.loads((target/'recording_manifest.json').read_text())
        self.assertEqual(manifest['metadata_sha256'], sha256(self.raw/'metadata.json'))
        with self.assertRaises(FileExistsError):
            migrate(self.raw, results_root=self.resultroot, apply=True)

    def test_split_resolver_never_points_to_raw_folder_after_migration(self):
        migrate(self.raw, results_root=self.resultroot, apply=True)
        with use_results_root(self.resultroot):
            derived = analysis_root(self.raw)
            self.assertFalse(self.raw in derived.parents)
            (self.raw/'metadata.json').write_text('{"modified": true}\n')
            with self.assertRaisesRegex(ValueError, 'metadata differs'):
                analysis_root(self.raw)

    def test_publish_curated_reports_only_and_preserve_source(self):
        migrate(self.raw, results_root=self.resultroot, apply=True)
        before = sha256(self.raw/'waveforms/ch1.csv')
        publish_root = self.home/'reports'
        with use_results_root(self.resultroot):
            target = publish(self.raw, reports_root=publish_root, tag='review-v1')
        self.assertTrue((target/'figures/plot.png').exists())
        self.assertTrue((target/'final_experiment_report.md').exists())
        self.assertTrue((target/'extended_analysis_technical_report.md').exists())
        text = (target/'final_experiment_report.md').read_text()
        self.assertIn('![Figure 1](figures/plot.png)', text)
        self.assertIn('local supporting evidence:', text)
        self.assertNotIn('[Detailed evidence](', text)
        self.assertFalse((target/'waveforms').exists())
        self.assertFalse((target/'pulse_review').exists())
        self.assertFalse((target/'command_timing').exists())
        self.assertEqual(before, sha256(self.raw/'waveforms/ch1.csv'))
        pub = json.loads((target/'publication_manifest.json').read_text())
        self.assertFalse(pub['raw_data_included'])
        self.assertFalse(pub['signed_review_included'])
        with self.assertRaises(FileExistsError):
            publish(self.raw, reports_root=publish_root, tag='review-v1',
                    analysis_path=self.resultroot/'sample_run'/'analysis')

    def test_traversal_and_symlink_escapes_are_rejected(self):
        folder = self.raw/'analysis'
        with self.assertRaisesRegex(ValueError, 'leaves analysis root'):
            _safe_relative(folder, '../metadata.json')
        with self.assertRaisesRegex(ValueError, 'Absolute local'):
            _safe_relative(folder, '/tmp/x.png')
        (folder/'symlink').symlink_to(self.raw/'metadata.json')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            migrate(self.raw, results_root=self.resultroot)

    def test_new_clean_recording_is_registered_without_raw_writes(self):
        import shutil
        shutil.rmtree(self.raw/'analysis')
        original = sha256(self.raw/'waveforms/ch1.csv')
        result = register(self.raw, results_root=self.resultroot)
        self.assertTrue(result.exists())
        self.assertEqual(original, sha256(self.raw/'waveforms/ch1.csv'))
        self.assertFalse((self.raw/'analysis').exists())
        self.assertEqual(register(self.raw, results_root=self.resultroot),result)
        with use_results_root(self.resultroot):
            self.assertEqual(analysis_root(self.raw),
                             self.resultroot/'sample_run'/'analysis')

    def test_legacy_run_requires_migration_before_register(self):
        with self.assertRaisesRegex(ValueError, 'migrate'):
            register(self.raw, results_root=self.resultroot)

    def test_registered_results_cannot_masquerade_as_signed_migration(self):
        # A pre-created results directory may have a registration manifest
        # but lack the signed review copied from raw/analysis.
        legacy = self.raw / 'analysis'
        holding = self.home / 'held_analysis'
        legacy.rename(holding)
        try:
            register(self.raw, results_root=self.resultroot)
        finally:
            holding.rename(legacy)
        with self.assertRaisesRegex(ValueError, 'not a completed'):
            verify_migration(self.raw, results_root=self.resultroot)
        with self.assertRaisesRegex(ValueError, 'has not been migrated'):
            register(self.raw, results_root=self.resultroot)
        with use_results_root(self.resultroot):
            with self.assertRaisesRegex(ValueError, 'does not record a completed'):
                analysis_root(self.raw)
        self.assertTrue((legacy / 'pulse_review/review_approval.json').exists())

    def test_verified_finalization_moves_only_embedded_analysis(self):
        migrate(self.raw, results_root=self.resultroot, apply=True)
        original_ch1 = sha256(self.raw/'waveforms/ch1.csv')
        original_signed = sha256(
            self.raw/'analysis/pulse_review/review_approval.json')
        preview = finalize(self.raw, results_root=self.resultroot)
        self.assertEqual(preview['status'], 'READY_TO_ARCHIVE')
        self.assertTrue((self.raw/'analysis').is_dir())
        final = finalize(self.raw, results_root=self.resultroot, apply=True)
        self.assertEqual(final['status'], 'ARCHIVED')
        self.assertFalse((self.raw/'analysis').exists())
        archive = self.resultroot/'sample_run'/'_legacy_source_analysis'
        self.assertEqual(sha256(archive/'pulse_review/review_approval.json'),
                         original_signed)
        self.assertEqual(sha256(self.raw/'waveforms/ch1.csv'), original_ch1)
        self.assertTrue((self.resultroot/'sample_run'/
                         'legacy_analysis_archive_receipt.json').is_file())
        self.assertEqual(verify_migration(
            self.raw, results_root=self.resultroot)['status'], 'VERIFIED')
        self.assertEqual(finalize(
            self.raw, results_root=self.resultroot)['status'], 'ALREADY_ARCHIVED')

    def test_finalizer_does_not_move_unmigrated_raw_approval(self):
        with self.assertRaises(FileNotFoundError):
            finalize(self.raw, results_root=self.resultroot)
        self.assertTrue(
            (self.raw/'analysis/pulse_review/review_approval.json').is_file())

    def test_finalizer_rejects_changed_original_signed_review(self):
        migrate(self.raw, results_root=self.resultroot, apply=True)
        (self.raw/'analysis/pulse_review/review_approval.json').write_text(
            'changed human decisions')
        with self.assertRaisesRegex(ValueError, 'Original human review'):
            finalize(self.raw, results_root=self.resultroot, apply=True)
        self.assertTrue((self.raw/'analysis').is_dir())
        self.assertFalse(
            (self.resultroot/'sample_run'/'_legacy_source_analysis').exists())

    def test_legacy_science_module_imports_alias_canonical_modules(self):
        import importlib
        for name in ('analyze_command_timing', 'analyze_frequency_transitions',
                     'analyze_stimulation_shutdown', 'build_final_experiment_report',
                     'audit_ack_physical_observability'):
            canonical = importlib.import_module('experiment_analysis.' + name)
            compatibility = importlib.import_module('oscilloscope.' + name)
            self.assertIs(canonical, compatibility, name)

    def test_root_override_context_is_restored(self):
        prior = os.environ.get('MTB4_RESULTS_ROOT')
        with use_results_root(self.resultroot):
            self.assertEqual(Path(os.environ['MTB4_RESULTS_ROOT']),self.resultroot)
        self.assertEqual(os.environ.get('MTB4_RESULTS_ROOT'),prior)


if __name__ == '__main__':
    unittest.main()
