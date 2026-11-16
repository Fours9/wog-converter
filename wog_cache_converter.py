"""Recover a UnityFS bundle from a World of Guns cache file or exported .bytes."""
import argparse
import itertools
import lzma
import re
from pathlib import Path
import struct
import sys

MAGIC = b"UnityFS\0"
KNOWN_KEYS = (b"1ea55a244e33ecc8e6abb782e3d1bea9", b"31e2fc5fb642aac8f5e687cdacbc2502")


def lz4(data, expected):
    output = bytearray()
    cursor = 0

    def length(value):
        nonlocal cursor
        if value == 15:
            while True:
                extra = data[cursor]
                cursor += 1
                value += extra
                if extra != 255:
                    break
        return value

    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        count = length(token >> 4)
        if cursor + count > len(data) or len(output) + count > expected:
            raise ValueError("Invalid LZ4 literal length")
        output.extend(data[cursor:cursor + count])
        cursor += count
        if cursor == len(data):
            break
        distance = struct.unpack_from("<H", data, cursor)[0]
        cursor += 2
        count = length(token & 15) + 4
        if not 0 < distance <= len(output) or len(output) + count > expected:
            raise ValueError("Invalid LZ4 match")
        output.extend((output[-distance:] * ((count + distance - 1) // distance))[:count])
    return output


def decompress(data, size, flags):
    codec = flags & 63
    if codec == 0:
        result = data
    elif codec == 1:
        props, dictionary = struct.unpack_from("<BI", data)
        lp_pb, lc = divmod(props, 9)
        pb, lp = divmod(lp_pb, 5)
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA1, "dict_size": dictionary,
            "lc": lc, "lp": lp, "pb": pb,
        }])
        result = decoder.decompress(data[5:], max_length=size)
    elif codec in (2, 3):
        result = lz4(data, size)
    else:
        raise ValueError(f"Unsupported UnityFS compression {codec}")
    if len(result) != size:
        raise ValueError("Unexpected decompressed block size")
    return result


def header(data):
    if not data.startswith(MAGIC):
        raise ValueError("Missing UnityFS signature")
    version = struct.unpack_from(">I", data, 8)[0]
    offset = data.index(0, data.index(0, 12) + 1) + 1
    size, packed, unpacked, flags = struct.unpack_from(">QIII", data, offset)
    offset += 20
    if version >= 7:
        offset = (offset + 15) & ~15
    return size, packed, unpacked, flags, offset


def unpack_bundle(data):
    size, packed, unpacked, flags, start = header(data)
    if size != len(data) or packed > size - start:
        raise ValueError("Invalid UnityFS size")
    table_at = size - packed if flags & 0x80 else start
    table = decompress(data[table_at:table_at + packed], unpacked, flags)
    cursor = start if flags & 0x80 else start + packed
    if flags & 0x200:
        cursor = (cursor + 15) & ~15
    end = table_at if flags & 0x80 else size
    block_count = struct.unpack_from(">I", table, 16)[0]
    if block_count > (len(table) - 20) // 10:
        raise ValueError("Invalid UnityFS block count")
    output = bytearray()
    for index in range(block_count):
        raw_size, compressed_size, block_flags = struct.unpack_from(">IIH", table, 20 + index * 10)
        if cursor + compressed_size > end:
            raise ValueError("UnityFS block exceeds file")
        output.extend(decompress(data[cursor:cursor + compressed_size], raw_size, block_flags))
        cursor += compressed_size
    if cursor != end:
        raise ValueError("Unexpected data after UnityFS blocks")
    return output


def xor(data, key):
    mask = (key * ((len(data) + len(key) - 1) // len(key)))[:len(data)]
    return (int.from_bytes(data, "little") ^ int.from_bytes(mask, "little")).to_bytes(len(data), "little")


def recover_with_key(source, wrapped, key):
    marker = xor(MAGIC, key)
    start = source.find(marker)
    while start >= 0:
        try:
            size, *_ = header(xor(source[start:start + 256], key))
            if 50 <= size <= len(source) - start and (wrapped or size == len(source)):
                candidate = xor(source[start:start + size], key)
                unpack_bundle(candidate)
                return candidate
        except (ValueError, IndexError, struct.error, lzma.LZMAError):
            pass
        start = source.find(marker, start + 1)
    return None


HEX = b"0123456789abcdefABCDEF"
UNITY_PREFIXES = tuple(
    MAGIC + struct.pack(">I", version) + b"5.x.x\0" for version in (6, 7, 8)
)


def infer_keys(source, wrapped):
    """Infer a repeating hexadecimal key from the header and payload size."""
    for prefix in UNITY_PREFIXES:
        pattern = b"".join(
            b"[" + re.escape(bytes(value ^ digit for digit in HEX)) + b"]"
            for value in prefix
        )
        for match in re.finditer(pattern, source):
            start = match.start()
            if wrapped and start < 4:
                continue
            size = struct.unpack_from("<I", source, start - 4)[0] if wrapped else len(source)
            if not 50 <= size <= len(source) - start:
                continue
            tail = struct.pack(">Q", size)
            cipher = source[start:start + 32]
            allowed = [set(value ^ digit for digit in HEX) for value in cipher]
            for width in (1, 2, 3):
                version_chars = (b"2", b"056", b"0123456789", b"0123456789",
                                 b".", b"0123456789", b".") + (b"0123456789",) * width
                version_chars += (b"fapb", b"0123456789")
                fields = version_chars + (b"\0",) + tuple(bytes([v]) for v in tail)
                options = [bytes(v for v in field if v in allowed[i])
                           for i, field in enumerate(fields[:32 - len(prefix)], len(prefix))]
                for suffix in itertools.product(*options):
                    plain = prefix + bytes(suffix)
                    key = bytes(a ^ b for a, b in zip(cipher, plain))
                    if len(key) == 32:
                        yield start, size, key


def try_inferred(source, wrapped):
    for start, size, key in infer_keys(source, wrapped):
        try:
            probe = xor(source[start:start + 4096], key)
            declared, packed, unpacked, flags, offset = header(probe)
            if declared != size or not 20 <= unpacked <= 65536:
                continue
            if flags & 0x80:
                continue
            if offset + packed > len(probe):
                continue
            table = decompress(probe[offset:offset + packed], unpacked, flags)
            if not re.search(rb"CAB-[0-9a-fA-F]{32}\0", table):
                continue
            count = struct.unpack_from(">I", table, 16)[0]
            if not 0 < count <= (len(table) - 20) // 10:
                continue
            block_start = offset + packed
            if flags & 0x200:
                block_start = (block_start + 15) & ~15
            blocks = [struct.unpack_from(">IIH", table, 20 + i * 10) for i in range(count)]
            if sum(c for _, c, _ in blocks) != size - block_start:
                continue
            candidate = xor(source[start:start + size], key)
            unpack_bundle(candidate)
            return candidate
        except (ValueError, IndexError, struct.error, lzma.LZMAError):
            continue
    return None


def recover(data, key=None):
    wrapped = data.startswith(MAGIC)
    source = unpack_bundle(data) if wrapped else data
    for candidate_key in (key,) if key else KNOWN_KEYS:
        result = recover_with_key(source, wrapped, candidate_key)
        if result is not None:
            return result
    if key is None:
        result = try_inferred(source, wrapped)
        if result is not None:
            return result
    raise ValueError("No valid inner bundle with the available keys.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Cache __data file or AssetRipper .bytes")
    parser.add_argument("destination", type=Path, help="New .unity3d file")
    parser.add_argument("--key", help="Optional 32-character MD5 text key")
    args = parser.parse_args()
    if args.key and (len(args.key) != 32 or any(c not in "0123456789abcdefABCDEF" for c in args.key)):
        parser.error("--key must contain exactly 32 hexadecimal characters")
    try:
        result = recover(args.source.read_bytes(), args.key.encode("ascii") if args.key else None)
        with args.destination.open("xb") as stream:
            stream.write(result)
    except (OSError, ValueError, IndexError, struct.error, lzma.LZMAError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Saved {args.destination} ({len(result):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
