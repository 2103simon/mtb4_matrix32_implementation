"""Source-compatible Matrix32 onset-detection primitives, copied unchanged.

Provenance: 2103simon/matrix32_nerve_stimulation,
branch cleanup/analysis-consolidation,
experiments/analysis_primitives/{baseline,negative_events,
record_start_rejection,suppressed_crossing_association,period_classification}.py

The source implementations are kept verbatim so the experimental review
workflow does not silently invent different event classification rules.
They have not been independently qualified for this project's 20-us
sample spacing or changing 2..400-Hz input; inspect each record first.
"""
