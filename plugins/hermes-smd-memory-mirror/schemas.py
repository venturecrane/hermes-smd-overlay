"""D1 table shapes for mirrored Honcho conclusions.

Stub. §7 ports from ss-console/ai-employee/adapter/memory/.

Two tables:
- persona_observations — live mirror of current Honcho conclusions with
  provenance columns (source_message_ids, confidence, evidence_status,
  mirrored_at).
- persona_observations_archive — TTL'd rows aged out of Honcho by
  archive.py; Captain can restore from here.
"""

# Placeholder. Replaced by the ported CREATE TABLE statements in §7.
PERSONA_OBSERVATIONS_DDL: str = ""

# Placeholder. Replaced by the ported CREATE TABLE statements in §7.
PERSONA_OBSERVATIONS_ARCHIVE_DDL: str = ""
