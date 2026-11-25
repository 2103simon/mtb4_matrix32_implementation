# Published Matrix32 experiment reports

This directory is intentionally **tracked by Git**. A published snapshot is a
small, permanent reference containing the **authoritative scientific report**,
the **detailed technical consolidation**, and **all images embedded in those
two reports**. It does **not** contain raw scope CSVs, MCU/BLE logs, signed
approval files, or the regenerable analysis tables.

Snapshots live at `reports/<RUN_ID>/<TAG>/`. Each snapshot has a README
and `publication_manifest.json` recording source and published hashes. A
tag cannot be overwritten by the publisher; use another tag for revisions.

Six original standalone MCU plots from 19 November 2026 are preserved
byte-for-byte under [legacy_mcu_plots/](legacy_mcu_plots/README.md); they are
not associated with the signed 20-second Matrix32 publication.

See [the migration and publication guide](../docs/recording_results_separation.md)
for the workflow. Generated outputs under `results/` are Git-ignored;
the deliberate `reports/` snapshots are not.
