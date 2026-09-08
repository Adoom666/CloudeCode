"""What unit an offset or a body size is expressed in. ONE definition.

This module exists because two sides that each spell their own unit will
eventually disagree, and nothing will notice: two fields named
``match_offset`` in one API meaning two different things is a defect a
client discovers by masking the wrong range of a credential. So the
secrets block and the search hits quote the SAME dict, and a change here
is a visible contract change everywhere at once.
"""

from __future__ import annotations

from typing import Dict

# ---------------------------------------------------------------------------
# Offset units - ONE definition, because two sides that each spell their own
# unit will eventually disagree and nothing will notice.
# ---------------------------------------------------------------------------
#
# MEASURED 2026-08-31 against the live dev corpus (12,390 findings over
# 6,240 bodies), NOT reasoned about. Every offset this archive returns -
# ``message_secret_findings.match_offset`` and the ``INSTR``-derived
# ``match_offset`` on a search hit alike - is a count of UNICODE CODE
# POINTS, and both producers make it so by construction:
#
#   - secrets: ``message_model_secrets.scan_text`` runs ``re.finditer``
#     over a Python ``str``, so ``match.start()`` and ``len(value)`` are
#     code-point counts.
#   - search: SQLite's ``INSTR``/``SUBSTR``/``LENGTH`` on a TEXT value
#     are character-based. Verified on this build (sqlite 3.53.4): for a
#     body whose match sits at code point 5 and byte 8, ``INSTR``
#     returned 5, and ``LENGTH(x)`` returned 30 against
#     ``LENGTH(CAST(x AS BLOB))`` 33.
#
# Slicing the stored text by ``[match_offset : match_offset + match_length]``
# reproduced the recorded ``value_sha256`` on a CODE POINT slice for
# 12,390 of 12,390 findings and on a BYTE slice for 0 of the 5,821 where
# the two interpretations differ. Zero findings matched neither. The
# check was positive-controlled: fed a synthetic row whose offset really
# is a byte offset, the same classifier reports "byte", so a byte result
# was reachable and simply did not occur.
OFFSET_UNITS_CODE_POINTS = "unicode_code_points"

#: What a JavaScript client actually indexes with. A JS string is UTF-16,
#: so ``String.prototype.slice`` counts an astral-plane character (emoji,
#: rare CJK) as TWO units while Python counts it as one. MEASURED: 1,100
#: of 12,390 findings sit in a body carrying an astral character BEFORE
#: the match, where a client masking with the raw code-point offset
#: misaligns and exposes the head of the credential. That is why the
#: secrets block carries a UTF-16 pair as well as the stored one.
OFFSET_UNITS_UTF16 = "utf16_code_units"

#: ``body_bytes`` is a CODE POINT count, not a byte count. It comes from
#: SQLite ``LENGTH(body_json)`` on a TEXT value. The name predates the
#: measurement and is now a documented lie kept only for compatibility;
#: ``body_chars`` carries the same number under a name that does not have
#: to be re-checked against the code to be believed. New clients read
#: ``body_chars``. Note this also means MAX_BODY_BYTES gates on
#: characters, so a body of mostly multi-byte text is admitted at up to
#: roughly 4x that many real bytes.
BODY_SIZE_UNITS = OFFSET_UNITS_CODE_POINTS


def offset_units_meta() -> Dict[str, str]:
    """The unit declaration that goes in ``meta`` on every response
    carrying an offset or a body size.

    Description: the single construction site, so the secrets block and
      the search hits cannot drift into meaning different things under
      the same field name. A client must never have to infer a unit.
    Inputs: none.
    Output: dict of unit declarations, all values stable strings.
    Example: offset_units_meta()["offset_units"] -> "unicode_code_points"
    """
    return {
        "offset_units": OFFSET_UNITS_CODE_POINTS,
        "offset_units_utf16_available": True,
        "body_size_units": BODY_SIZE_UNITS,
        "body_bytes_units": BODY_SIZE_UNITS,
        "body_bytes_note": (
            "body_bytes is a DEPRECATED alias for body_chars and counts "
            "UNICODE CODE POINTS, not bytes. Read body_chars."
        ),
    }
