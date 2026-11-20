"""Nano Every v1 binary SYNC envelope, interoperable with text FRAME logs.

Wire order little-endian. CRC is IEEE CRC-32 (zlib.crc32) over
version/type/sequence/payload (everything AFTER the two magic bytes
and BEFORE the four CRC bytes).

Request:  A5 5A | 01 | 01 | seq:u32 | crc32:u32       (12 bytes)
Response: A5 5A | 01 | 02 | seq:u32 | RX_US:u32 |
          TX_US:u32 | crc32:u32                      (20 bytes)

The separate textual FRAME/PING/START/STOP protocol remains operational.
Byte-stream framing does NOT imply hardware timestamp precision.
"""
from __future__ import annotations

import struct
import zlib

MAGIC = b"\xa5\x5a"
VERSION = 1
SYNC_REQUEST = 1
SYNC_ACK = 2
REQUEST_SIZE = 12
ACK_SIZE = 20
MAX_LINE = 512


class BinaryProtocolError(ValueError):
    pass


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def pack_sync_request(sequence: int) -> bytes:
    if not (0 <= sequence <= 0xFFFFFFFF):
        raise ValueError("Sequence must fit uint32")
    body = struct.pack("<BBI", VERSION, SYNC_REQUEST, sequence)
    return MAGIC + body + struct.pack("<I", crc32(body))


def pack_sync_ack(sequence: int, rx_us: int, tx_us: int) -> bytes:
    for value in (sequence, rx_us, tx_us):
        if not (0 <= value <= 0xFFFFFFFF):
            raise ValueError("SYNC fields must fit uint32")
    body = struct.pack("<BBIII", VERSION, SYNC_ACK, sequence, rx_us, tx_us)
    return MAGIC + body + struct.pack("<I", crc32(body))


def unpack_sync_ack(frame: bytes) -> tuple[int, int, int]:
    if len(frame) != ACK_SIZE or frame[:2] != MAGIC:
        raise BinaryProtocolError("Invalid binary ACK envelope")
    body = frame[2:-4]
    got, = struct.unpack("<I", frame[-4:])
    if crc32(body) != got:
        raise BinaryProtocolError("CRC32 mismatch in binary ACK")
    version, kind, seq, rx, tx = struct.unpack("<BBIII", body)
    if version != VERSION or kind != SYNC_ACK:
        raise BinaryProtocolError("Unsupported Nano binary ACK version or type")
    return seq, rx, tx


class MixedNanoStreamDecoder:
    """Incrementally parse binary ACK frames and ASCII newline logs.

    A CRC-bad binary envelope is discarded and never routed as an ACK.
    Text is constrained to a maximum line length to bound memory usage.
    """
    def __init__(self):
        self._buffer = bytearray()
        self.bad_crc_count = 0
        self.discarded_byte_count = 0

    def feed(self, data: bytes):
        self._buffer.extend(data)
        output = []
        while self._buffer:
            if self._buffer[0] == MAGIC[0]:
                if len(self._buffer) == 1:
                    break
                if self._buffer[:2] == MAGIC:
                    if len(self._buffer) < 4:
                        break
                    size = (ACK_SIZE if self._buffer[3] == SYNC_ACK
                            else REQUEST_SIZE if self._buffer[3] == SYNC_REQUEST
                            else None)
                    if size is None:
                        del self._buffer[0]
                        self.discarded_byte_count += 1
                        continue
                    if len(self._buffer) < size:
                        break
                    frame = bytes(self._buffer[:size])
                    del self._buffer[:size]
                    if frame[3] != SYNC_ACK:
                        self.discarded_byte_count += size
                        continue
                    try:
                        result = unpack_sync_ack(frame)
                    except BinaryProtocolError:
                        self.bad_crc_count += 1
                    else:
                        output.append(("binary_sync_ack", result))
                    continue
            newline = self._buffer.find(b"\n")
            next_magic = self._buffer.find(MAGIC)
            if newline >= 0 and (next_magic < 0 or newline < next_magic):
                line = bytes(self._buffer[:newline]).strip()
                del self._buffer[:newline+1]
                if line:
                    output.append(("text", line.decode("ascii", errors="replace")))
                continue
            if next_magic > 0:
                # A partial or malformed text line before a binary message.
                del self._buffer[:next_magic]
                self.discarded_byte_count += next_magic
                continue
            if len(self._buffer) > MAX_LINE:
                del self._buffer[:-1]
                self.discarded_byte_count += MAX_LINE
                continue
            break
        return output
