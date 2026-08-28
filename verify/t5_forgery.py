"""D2 ROW-DELIMITER FORGERY: one tmux session becomes N parser rows.

tmux 3.7b rejects \n \r \v \f \x1c \x1d \x1e in a session name.
It ACCEPTS \x85 (NEL),   (LS) and   (PS).
Python's str.splitlines() treats all ten as line boundaries.

tmux_backend.list_attachable_sessions does raw_lines = stdout_text.splitlines(),
so the three tmux accepts are row injectors the D2 fix never considered:
it hardened the FIELD delimiter and proved that, while the ROW delimiter
stayed a wider alphabet than tmux validates against.
"""
import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import sys
_add_repo_root()
from src.core.tmux_listing_parse import parse_listing_row, resolve_ownership

SEP = " "          # also works with "\x85" and " "
REAL_EPOCH = 1755000000
owned_instances = {("cloude_work", REAL_EPOCH)}   # what the DB legitimately holds


def rows_from(one_session_name, sid="$7", epoch=1787000000):
    """Render ONE tmux listing line, then split it the way production does."""
    stdout_text = f"{sid}|{epoch}|1|{one_session_name}\n"
    print(f"  tmux emitted {stdout_text.count(chr(10))} real line(s); "
          f"python splitlines() yields {len(stdout_text.splitlines())} row(s)")
    return stdout_text.splitlines()


print("=== SCENARIO A: forge a row that badges as OURS ===")
attack = f"pwn{SEP}$99|{REAL_EPOCH}|1|cloude_work"
for r in rows_from(attack):
    p = parse_listing_row(r)
    if p is None:
        print(f"    REJECTED  (the user's REAL session vanishes from the list): {r!r}")
    else:
        own = resolve_ownership(p["name"], p["created_at_epoch"],
                                owned_instances, None, prefix="cloude_")
        print(f"    PARSED    name={p['name']!r} id={p['session_id']} "
              f"epoch={p['created_at_epoch']} -> created_by_cloude={own}")

print("\n=== SCENARIO B: poison a name the user owns (tier-2 negative) ===")
attack2 = f"pwn{SEP}$99|{REAL_EPOCH + 1}|1|cloude_work"
for r in rows_from(attack2, sid="$8"):
    p = parse_listing_row(r)
    if p:
        own = resolve_ownership(p["name"], p["created_at_epoch"],
                                owned_instances, None, prefix="cloude_")
        print(f"    PARSED    name={p['name']!r} epoch={p['created_at_epoch']} -> owned={own}")

print("\n=== SCENARIO C: N forged rows from ONE session ===")
multi = f"a{SEP}$1|1|1|forged_one{SEP}$2|2|1|forged_two{SEP}$3|3|1|forged_three"
rows = rows_from(multi, sid="$9")
good = [parse_listing_row(r) for r in rows]
print("    valid forgeries:", [g["name"] for g in good if g])

print("\n=== SCENARIO D: which separators work against tmux 3.7b ===")
for label, ch in (("\\x85 NEL", "\x85"), ("\\u2028 LS", " "), ("\\u2029 PS", " ")):
    r = f"$5|1|1|x{ch}$99|{REAL_EPOCH}|1|cloude_work\n".splitlines()
    forged = [parse_listing_row(x) for x in r]
    print(f"    {label:11} -> rows={len(r)} forged={[f['name'] for f in forged if f]}")
