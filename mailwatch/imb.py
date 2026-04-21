"""USPS Intelligent Mail Barcode (IMb) encoder.

Ported from the original Simplified-BSD Python reference implementation
(https://www.opensource.org/licenses/bsd-license.html). The algorithm follows
USPS-B-3200 / USPS-STD-6A and produces a 65-character FADT string:

    F = Full / Both ascender + descender
    A = Ascender only
    D = Descender only
    T = Tracker (neither)

Spec: https://ribbs.usps.gov/intelligentmail_mailpieces/documents/tech_guides/SPUSPSG.pdf

The algorithm, permutation tables, and CRC-11 logic are preserved verbatim.
Port changes are limited to: type annotations, docstrings, exceptions in place
of stderr printing, and removal of the CLI entry point and decoding helpers
(this module is write-only for mailwatch).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Spec-defined constants
# ---------------------------------------------------------------------------

# Maximum value of the 2-digit Barcode ID field (USPS-B-3200).
_MAX_BARCODE_ID = 94
# Maximum value of the 3-digit Service Type Identifier field.
_MAX_SERVICE_TYPE = 999
# Permitted routing-code lengths (delivery-point ZIP).
_ROUTING_LEN_5 = 5
_ROUTING_LEN_9 = 9
_ROUTING_LEN_11 = 11
# Tracking code width (2+3+6+9 = 2+3+9+6 = 20 digits).
_TRACKING_LEN = 20
# Boundary between the tab5 (5-of-13) and tab2 (2-of-13) codeword ranges.
_TAB5_SIZE = 1287
_TAB2_MAX_INDEX = 1364  # inclusive (_TAB5_SIZE + 78 - 1)


# ---------------------------------------------------------------------------
# CRC-11, bit/byte utilities
# ---------------------------------------------------------------------------


def _crc11(data: list[int]) -> int:
    """Compute USPS-B-3200 CRC-11 frame check sequence over 13 input bytes."""
    gen_poly = 0x0F35
    fcs = 0x07FF
    byte = data[0] << 5
    # most significant byte: skip the 2 most significant bits
    for _ in range(2, 8):
        if (fcs ^ byte) & 0x400:
            fcs = (fcs << 1) ^ gen_poly
        else:
            fcs = fcs << 1
        fcs &= 0x7FF
        byte <<= 1
    # remaining bytes
    for byte_index in range(1, 13):
        byte = data[byte_index] << 3
        for _ in range(8):
            if (fcs ^ byte) & 0x400:
                fcs = (fcs << 1) ^ gen_poly
            else:
                fcs = fcs << 1
            fcs &= 0x7FF
            byte <<= 1
    return fcs


def _reverse_int16(value: int) -> int:
    """Reverse the bit order of a 16-bit integer."""
    reverse = 0
    for _ in range(16):
        reverse <<= 1
        reverse |= value & 1
        value >>= 1
    return reverse


def _to_bytes(val: int, nbytes: int) -> list[int]:
    """Convert a non-negative integer to a big-endian list of ``nbytes`` bytes."""
    result = []
    for _ in range(nbytes):
        result.append(val & 0xFF)
        val >>= 8
    result.reverse()
    return result


# ---------------------------------------------------------------------------
# 13-bit permutation tables (n-of-13 error-detection encoding)
# ---------------------------------------------------------------------------


def _init_n_of_13(n: int, table_length: int) -> dict[int, int]:
    """Build the n-of-13 permutation table used by the USPS spec."""
    # Mirrors the reference implementation; the spec does not explain the
    # construction, but empirically this produces the tab5 (1287 entries) and
    # tab2 (78 entries) permutation tables that `encode` selects from.
    table: dict[int, int] = {}
    index_low = 0
    index_hi = table_length - 1
    for i in range(8192):
        bit_count = bin(i).count("1")
        if bit_count != n:
            continue
        reverse = _reverse_int16(i) >> 3
        if reverse < i:
            continue
        if i == reverse:
            table[index_hi] = i
            index_hi -= 1
        else:
            table[index_low] = i
            index_low += 1
            table[index_low] = reverse
            index_low += 1
    if index_low != index_hi + 1:
        raise ValueError(f"n-of-13 table construction failed: {index_low=} {index_hi=}")
    return table


# ---------------------------------------------------------------------------
# Bar-position table (from the USPS spec)
# ---------------------------------------------------------------------------

_BAR_TABLE: tuple[str, ...] = (
    "H 2 E 3", "B 10 A 0", "J 12 C 8", "F 5 G 11", "I 9 D 1",
    "A 1 F 12", "C 5 B 8", "E 4 J 11", "G 3 I 10", "D 9 H 6",
    "F 11 B 4", "I 5 C 12", "J 10 A 2", "H 1 G 7", "D 6 E 9",
    "A 3 I 6", "G 4 C 7", "B 1 J 9", "H 10 F 2", "E 0 D 8",
    "G 2 A 4", "I 11 B 0", "J 8 D 12", "C 6 H 7", "F 1 E 10",
    "B 12 G 9", "H 3 I 0", "F 8 J 7", "E 6 C 10", "D 4 A 5",
    "I 4 F 7", "H 11 B 9", "G 0 J 6", "A 6 E 8", "C 1 D 2",
    "F 9 I 12", "E 11 G 1", "J 5 H 4", "D 3 B 2", "A 7 C 0",
    "B 3 E 1", "G 10 D 5", "I 7 J 4", "C 11 F 6", "A 8 H 12",
    "E 2 I 1", "F 10 D 0", "J 3 A 9", "G 5 C 4", "H 8 B 7",
    "F 0 E 5", "C 3 A 10", "G 12 J 2", "D 11 B 6", "I 8 H 9",
    "F 4 A 11", "B 5 C 2", "J 1 E 12", "I 3 G 6", "H 0 D 7",
    "E 7 H 5", "A 12 B 11", "C 9 J 0", "G 8 F 3", "D 10 I 2",
)  # fmt: skip


def _process_bar_table() -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
    """Parse the spec bar-position table into ascender/descender lookup maps."""
    table_a: dict[int, tuple[int, int]] = {}
    table_d: dict[int, tuple[int, int]] = {}
    for i in range(65):
        entry = _BAR_TABLE[i]
        i0_s, d_s, i1_s, a_s = entry.split()
        i0 = ord(i0_s) - 65
        i1 = ord(i1_s) - 65
        table_d[i] = (i0, int(d_s))
        table_a[i] = (i1, int(a_s))
    return table_a, table_d


# Module-level precomputed tables (built once at import).
_TABLE_A, _TABLE_D = _process_bar_table()
_TAB5: dict[int, int] = _init_n_of_13(5, 1287)
_TAB2: dict[int, int] = _init_n_of_13(2, 78)


# ---------------------------------------------------------------------------
# Numeric conversion: routing zip + tracking digits -> 102-bit integer
# ---------------------------------------------------------------------------


def _convert_routing_code(zip_code: str) -> int:
    """Convert the 0/5/9/11-digit routing code to its USPS integer form."""
    length = len(zip_code)
    if length == 0:
        return 0
    if length == _ROUTING_LEN_5:
        return int(zip_code) + 1
    if length == _ROUTING_LEN_9:
        return int(zip_code) + 100000 + 1
    if length == _ROUTING_LEN_11:
        return int(zip_code) + 1000000000 + 100000 + 1
    raise ValueError(f"routing code must be 0, 5, 9, or 11 digits (got {length}: {zip_code!r})")


def _convert_tracking_code(enc: int, track: str) -> int:
    """Fold the 20-character tracking string into ``enc`` per the spec."""
    if len(track) != _TRACKING_LEN:
        raise ValueError(f"tracking code must be 20 digits (got {len(track)}: {track!r})")
    enc = (enc * 10) + int(track[0])
    enc = (enc * 5) + int(track[1])
    for i in range(2, _TRACKING_LEN):
        enc = (enc * 10) + int(track[i])
    return enc


def _binary_to_codewords(n: int) -> list[int]:
    """Split the 102-bit integer into 10 codewords (one mod-636, nine mod-1365)."""
    result = []
    n, x = divmod(n, 636)
    result.append(x)
    for _ in range(9):
        n, x = divmod(n, 1365)
        result.append(x)
    result.reverse()
    return result


# ---------------------------------------------------------------------------
# FADT rendering
# ---------------------------------------------------------------------------


def _codeword_to_character(b: int) -> int:
    """Map a single codeword (0..1364) to its 13-bit character from tab5/tab2."""
    if b < _TAB5_SIZE:
        return _TAB5[b]
    if _TAB5_SIZE <= b <= _TAB2_MAX_INDEX:
        return _TAB2[b - _TAB5_SIZE]
    raise ValueError(f"codeword out of range: {b}")


def _make_bars(code: list[int]) -> str:
    """Assemble the 65-character FADT string from 10 encoded characters."""
    result = []
    for i in range(65):
        index, bit = _TABLE_A[i]
        ascend = (code[index] & (1 << bit)) != 0
        index, bit = _TABLE_D[i]
        descend = (code[index] & (1 << bit)) != 0
        result.append("TADF"[descend << 1 | ascend])
    return "".join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def encode(
    barcode_id: int,
    service_type: int,
    mailer_id: int,
    serial: int,
    routing_code: str,
) -> str:
    """Encode USPS-B-3200 IMb inputs into a 65-character FADT barcode string.

    Args:
        barcode_id: 2-digit Barcode ID (STID value 0-93, first digit 0-9,
            second digit 0-4).
        service_type: 3-digit Service Type Identifier (0-999).
        mailer_id: 6-digit (starts 0-8) or 9-digit (starts 9) Mailer ID.
        serial: 9-digit or 6-digit serial number (complement of ``mailer_id``).
        routing_code: 0, 5, 9, or 11 digit destination delivery-point routing
            code (empty string is allowed).

    Returns:
        A 65-character string over the alphabet ``{F, A, D, T}``.

    Raises:
        ValueError: if any input is out of range or the routing code length
            is not one of 0, 5, 9, 11.
    """
    _validate_inputs(barcode_id, service_type, mailer_id, serial)

    n = _convert_routing_code(routing_code)
    if str(mailer_id)[0] == "9":
        tracking = f"{barcode_id:02d}{service_type:03d}{mailer_id:09d}{serial:06d}"
    else:
        tracking = f"{barcode_id:02d}{service_type:03d}{mailer_id:06d}{serial:09d}"
    n = _convert_tracking_code(n, tracking)

    # CRC-11 over the 13-byte big-endian representation of the 102-bit value.
    fcs = _crc11(_to_bytes(n, 13))
    codewords = _binary_to_codewords(n)
    codewords[9] *= 2
    if fcs & (1 << 10):
        codewords[0] += 659

    characters = [_codeword_to_character(b) for b in codewords]
    for i in range(10):
        if fcs & (1 << i):
            characters[i] = characters[i] ^ 0x1FFF
    return _make_bars(characters)


def _validate_inputs(barcode_id: int, service_type: int, mailer_id: int, serial: int) -> None:
    """Raise ``ValueError`` for any out-of-range field in ``encode``."""
    if not 0 <= barcode_id <= _MAX_BARCODE_ID:
        raise ValueError(f"barcode_id must be in 0..{_MAX_BARCODE_ID} (got {barcode_id})")
    if not 0 <= service_type <= _MAX_SERVICE_TYPE:
        raise ValueError(f"service_type must be in 0..{_MAX_SERVICE_TYPE} (got {service_type})")
    if mailer_id < 0:
        raise ValueError(f"mailer_id must be non-negative (got {mailer_id})")
    if serial < 0:
        raise ValueError(f"serial must be non-negative (got {serial})")
