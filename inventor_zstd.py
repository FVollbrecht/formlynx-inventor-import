# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Zstandard-Entpacker in reinem Python (RFC 8878), ohne bpy und ohne Fremdpakete.

Inventor 2026 packt RSeStorage-Segmente mit Zstandard; Blender-Python (3.11/3.13) hat dafür kein Modul.
Nur Entpacken, ohne Wörterbücher. Die Segmente sind klein (KB bis wenige MB), Geschwindigkeit reicht.
"""


class ZstdError(ValueError):
    pass


MAGIC = 0xFD2FB528
MAX_OUTPUT = 512 << 20

# Vordefinierte Verteilungen (RFC 8878, 3.1.1.3.2.2)
LL_DEFAULT = (6, [4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
                  -1, -1, -1, -1])
ML_DEFAULT = (6, [1, 4, 3, 2, 2, 2, 2, 2, 2] + [1] * 37 + [-1] * 7)
OF_DEFAULT = (5, [1, 1, 1, 1, 1, 1, 2, 2, 2] + [1] * 15 + [-1] * 5)

LL_BASE = list(range(16)) + [16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096, 8192,
                             16384, 32768, 65536]
LL_BITS = [0] * 16 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
ML_BASE = [c + 3 for c in range(32)] + [35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051,
                                        4099, 8195, 16387, 32771, 65539]
ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]


# ---------- Bitleser ----------

class _Forward:
    """Bits vom niedrigsten Bit des ersten Bytes an (Tabellenbeschreibungen)"""

    def __init__(self, data, start):
        self.d, self.bit = data, start * 8

    def peek(self, n):
        p = self.bit
        b = p >> 3
        return (int.from_bytes(self.d[b:b + ((p & 7) + n + 7) // 8], "little") >> (p & 7)) & ((1 << n) - 1)

    def skip(self, n):
        self.bit += n

    def byte_end(self):
        return (self.bit + 7) >> 3


class _Backward:
    """Rückwärts gelesener Bitstrom (Huffman, FSE): beginnt unter dem höchsten gesetzten Bit des letzten Bytes.
    Lesen über den Anfang hinaus liefert Nullen (pos wird negativ)."""

    def __init__(self, data):
        if not data or data[-1] == 0:
            raise ZstdError("bitstream without end mark")
        self.d = data
        self.pos = len(data) * 8 - 8 + data[-1].bit_length() - 1

    def peek(self, n):
        p = self.pos - n
        if p >= 0:
            b = p >> 3
            return (int.from_bytes(self.d[b:b + ((p & 7) + n + 7) // 8], "little") >> (p & 7)) & ((1 << n) - 1)
        have = self.pos
        if have <= 0:
            return 0
        v = int.from_bytes(self.d[0:(have + 7) // 8], "little") & ((1 << have) - 1)
        return v << (-p)

    def read(self, n):
        if n == 0:
            return 0
        v = self.peek(n)
        self.pos -= n
        return v


# ---------- FSE ----------

def _read_ncount(data, start, max_log, max_symbol):
    """FSE-Tabellenbeschreibung -> (accuracy_log, Häufigkeiten, Ende in Bytes)"""
    br = _Forward(data, start)
    log = br.peek(4) + 5
    br.skip(4)
    if log > max_log:
        raise ZstdError("FSE accuracy log too large")
    remaining = (1 << log) + 1
    threshold = 1 << log
    nbits = log + 1
    norm, symbol, previous0 = [], 0, False
    while remaining > 1 and symbol <= max_symbol:
        if previous0:
            n0 = symbol
            while br.peek(2) == 3:
                n0 += 3
                br.skip(2)
            n0 += br.peek(2)
            br.skip(2)
            while symbol < n0:
                norm.append(0)
                symbol += 1
            if symbol > max_symbol:
                break
        biggest = (2 * threshold - 1) - remaining
        low = br.peek(nbits - 1)
        if low < biggest:
            count = low
            br.skip(nbits - 1)
        else:
            count = br.peek(nbits)
            if count >= threshold:
                count -= biggest
            br.skip(nbits)
        count -= 1
        remaining -= -count if count < 0 else count
        norm.append(count)
        symbol += 1
        previous0 = count == 0
        while remaining < threshold:
            nbits -= 1
            threshold >>= 1
    if remaining != 1 or symbol > max_symbol + 1:
        raise ZstdError("corrupt FSE table description")
    return log, norm, br.byte_end()


def _fse_table(log, norm):
    """Dekodiertabelle: Listen (Symbol, Bitanzahl, Grundzustand) je Zustand"""
    size = 1 << log
    symbols = [0] * size
    high = size - 1
    nxt = [0] * len(norm)
    for s, c in enumerate(norm):
        if c == -1:
            symbols[high] = s
            high -= 1
            nxt[s] = 1
        else:
            nxt[s] = c
    step = (size >> 1) + (size >> 3) + 3
    mask, pos = size - 1, 0
    for s, c in enumerate(norm):
        for _ in range(max(c, 0)):
            symbols[pos] = s
            pos = (pos + step) & mask
            while pos > high:
                pos = (pos + step) & mask
    if pos != 0:
        raise ZstdError("corrupt FSE distribution")
    bits, base = [0] * size, [0] * size
    for u in range(size):
        s = symbols[u]
        n = nxt[s]
        nxt[s] += 1
        nb = log - (n.bit_length() - 1)
        bits[u] = nb
        base[u] = (n << nb) - size
    return log, symbols, bits, base


def _rle_table(symbol):
    return 0, [symbol], [0], [0]


# ---------- Huffman ----------

def _huffman_table(data, start):
    """Huffman-Baumbeschreibung -> ((max_bits, Symbole, Bitanzahl), Ende)"""
    head = data[start]
    pos = start + 1
    if head >= 128:
        count = head - 127
        end = pos + (count + 1) // 2
        if end > len(data):
            raise ZstdError("truncated Huffman weights")
        weights = []
        for i in range(count):
            byte = data[pos + i // 2]
            weights.append(byte >> 4 if i % 2 == 0 else byte & 15)
    else:
        end = pos + head
        if end > len(data):
            raise ZstdError("truncated Huffman weights")
        log, norm, used = _read_ncount(data, pos, 6, 255)
        table = _fse_table(log, norm)
        br = _Backward(data[used:end])
        _, syms, bits, base = table
        s1 = br.read(log)
        s2 = br.read(log)
        weights = []
        while True:
            weights.append(syms[s1])
            s1 = base[s1] + br.read(bits[s1])
            if br.pos < 0:
                weights.append(syms[s2])
                break
            weights.append(syms[s2])
            s2 = base[s2] + br.read(bits[s2])
            if br.pos < 0:
                weights.append(syms[s1])
                break
            if len(weights) > 255:
                raise ZstdError("too many Huffman weights")
    total = sum(1 << (w - 1) for w in weights if w)
    if total == 0:
        raise ZstdError("empty Huffman table")
    max_bits = total.bit_length()
    rest = (1 << max_bits) - total
    if rest & (rest - 1):
        raise ZstdError("corrupt Huffman weights")
    weights.append(rest.bit_length())
    if max_bits > 11:
        raise ZstdError("Huffman code too long")
    # Kanonische Zuordnung: kleine Gewichte (lange Codes) zuerst
    counts = [0] * (max_bits + 2)
    for w in weights:
        counts[w] += 1
    index = [0] * (max_bits + 2)
    acc = 0
    for w in range(1, max_bits + 2):
        index[w] = acc
        acc += counts[w] << (w - 1)
    size = 1 << max_bits
    symbols, nbits = [0] * size, [0] * size
    for s, w in enumerate(weights):
        if not w:
            continue
        n = 1 << (w - 1)
        i = index[w]
        nb = max_bits + 1 - w
        for k in range(i, i + n):
            symbols[k] = s
            nbits[k] = nb
        index[w] += n
    return (max_bits, symbols, nbits), end


def _huffman_stream(src, count, table, out):
    max_bits, symbols, nbits = table
    br = _Backward(src)
    for _ in range(count):
        k = br.peek(max_bits)
        out.append(symbols[k])
        br.pos -= nbits[k]
    if br.pos != 0:
        raise ZstdError("Huffman stream not fully consumed")


# ---------- Blöcke ----------

class _Frame:
    def __init__(self):
        self.huffman = None
        self.tables = [None, None, None]        # LL, OF, ML
        self.rep = [1, 4, 8]
        self.out = bytearray()

    def literals(self, data, pos):
        b0 = data[pos]
        kind, fmt = b0 & 3, (b0 >> 2) & 3
        if kind in (0, 1):
            if fmt in (0, 2):
                size, pos = b0 >> 3, pos + 1
            elif fmt == 1:
                size, pos = (b0 >> 4) + (data[pos + 1] << 4), pos + 2
            else:
                size, pos = (b0 >> 4) + (data[pos + 1] << 4) + (data[pos + 2] << 12), pos + 3
            if kind == 0:
                if pos + size > len(data):
                    raise ZstdError("truncated raw literals")
                return bytes(data[pos:pos + size]), pos + size
            return bytes([data[pos]]) * size, pos + 1
        nhead = (3, 3, 4, 5)[fmt]
        bits = (10, 10, 14, 18)[fmt]
        h = int.from_bytes(data[pos:pos + nhead], "little")
        regen = (h >> 4) & ((1 << bits) - 1)
        csize = (h >> (4 + bits)) & ((1 << bits) - 1)
        pos += nhead
        end = pos + csize
        if end > len(data):
            raise ZstdError("truncated compressed literals")
        if kind == 2:
            self.huffman, pos = _huffman_table(data, pos)
        elif self.huffman is None:
            raise ZstdError("treeless literals without previous table")
        out = []
        if fmt == 0:
            _huffman_stream(data[pos:end], regen, self.huffman, out)
        else:
            s1, s2, s3 = (int.from_bytes(data[pos + 2 * i:pos + 2 * i + 2], "little") for i in range(3))
            p = pos + 6
            part = (regen + 3) // 4
            sizes = (part, part, part, regen - 3 * part)
            for size, n in zip((s1, s2, s3, end - p - s1 - s2 - s3), sizes):
                if size < 0 or n < 0:
                    raise ZstdError("corrupt literal streams")
                _huffman_stream(data[p:p + size], n, self.huffman, out)
                p += size
        return bytes(out), end

    def _table(self, mode, data, pos, idx, default, max_log, max_symbol):
        if mode == 0:
            self.tables[idx] = _fse_table(*default)
        elif mode == 1:
            self.tables[idx] = _rle_table(data[pos])
            pos += 1
        elif mode == 2:
            log, norm, pos = _read_ncount(data, pos, max_log, max_symbol)
            self.tables[idx] = _fse_table(log, norm)
        elif self.tables[idx] is None:
            raise ZstdError("repeat table without previous table")
        return pos

    def block(self, data):
        lits, pos = self.literals(data, 0)
        b0 = data[pos]
        if b0 == 0:
            nseq, pos = 0, pos + 1
        elif b0 < 128:
            nseq, pos = b0, pos + 1
        elif b0 < 255:
            nseq, pos = ((b0 - 128) << 8) + data[pos + 1], pos + 2
        else:
            nseq, pos = data[pos + 1] + (data[pos + 2] << 8) + 0x7F00, pos + 3
        out = self.out
        if nseq == 0:
            out += lits
            return
        modes = data[pos]
        pos += 1
        pos = self._table(modes >> 6, data, pos, 0, LL_DEFAULT, 9, 35)
        pos = self._table((modes >> 4) & 3, data, pos, 1, OF_DEFAULT, 8, 31)
        pos = self._table((modes >> 2) & 3, data, pos, 2, ML_DEFAULT, 9, 52)
        (ll_log, ll_sym, ll_bits, ll_base) = self.tables[0]
        (of_log, of_sym, of_bits, of_base) = self.tables[1]
        (ml_log, ml_sym, ml_bits, ml_base) = self.tables[2]
        br = _Backward(data[pos:])
        read = br.read
        ll_s, of_s, ml_s = read(ll_log), read(of_log), read(ml_log)
        rep = self.rep
        lp = 0
        for n in range(nseq):
            of_code, ll_code, ml_code = of_sym[of_s], ll_sym[ll_s], ml_sym[ml_s]
            if of_code > 31:
                raise ZstdError("offset code too large")
            ofv = (1 << of_code) + read(of_code)
            ml = ML_BASE[ml_code] + read(ML_BITS[ml_code])
            ll = LL_BASE[ll_code] + read(LL_BITS[ll_code])
            if ofv > 3:
                offset = ofv - 3
                rep[2], rep[1], rep[0] = rep[1], rep[0], offset
            else:
                if ll == 0:
                    ofv += 1
                if ofv == 1:
                    offset = rep[0]
                elif ofv == 2:
                    offset = rep[1]
                    rep[1], rep[0] = rep[0], offset
                elif ofv == 3:
                    offset = rep[2]
                    rep[2], rep[1], rep[0] = rep[1], rep[0], offset
                else:
                    offset = rep[0] - 1
                    if offset <= 0:
                        raise ZstdError("invalid repeat offset")
                    rep[2], rep[1], rep[0] = rep[1], rep[0], offset
            if n != nseq - 1:
                ll_s = ll_base[ll_s] + read(ll_bits[ll_s])
                ml_s = ml_base[ml_s] + read(ml_bits[ml_s])
                of_s = of_base[of_s] + read(of_bits[of_s])
            if lp + ll > len(lits):
                raise ZstdError("literal length beyond literals")
            out += lits[lp:lp + ll]
            lp += ll
            start = len(out) - offset
            if start < 0:
                raise ZstdError("match offset before start of output")
            if offset >= ml:
                out += out[start:start + ml]
            else:                       # überlappende Kopie: Muster wiederholen
                chunk = out[start:]
                while ml > 0:
                    take = min(ml, len(chunk))
                    out += chunk[:take]
                    ml -= take
            if len(out) > MAX_OUTPUT:
                raise ZstdError("output too large")
        if br.pos != 0:
            raise ZstdError("sequence stream not fully consumed")
        out += lits[lp:]


def decompress(data, max_output=MAX_OUTPUT):
    """Entpackt einen oder mehrere Zstandard-Frames (überspringbare Frames werden ausgelassen)"""
    data = memoryview(bytes(data)) if not isinstance(data, (bytes, bytearray)) else data
    result = bytearray()
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        magic = int.from_bytes(data[pos:pos + 4], "little")
        if magic & 0xFFFFFFF0 == 0x184D2A50:
            size = int.from_bytes(data[pos + 4:pos + 8], "little")
            pos += 8 + size
            continue
        if magic != MAGIC:
            if result:
                break                   # Rest hinter den Frames ignorieren
            raise ZstdError("not a Zstandard frame")
        pos += 4
        fhd = data[pos]
        pos += 1
        fcs_flag, single, checksum, dict_flag = fhd >> 6, (fhd >> 5) & 1, (fhd >> 2) & 1, fhd & 3
        if fhd & 8:
            raise ZstdError("reserved bit set in frame header")
        if not single:
            pos += 1                    # Fensterbeschreibung: ganzer Ausgang bleibt im Speicher
        if dict_flag:
            if int.from_bytes(data[pos:pos + (0, 1, 2, 4)[dict_flag]], "little"):
                raise ZstdError("Zstandard dictionaries are not supported")
            pos += (0, 1, 2, 4)[dict_flag]
        pos += (1 if single else 0, 2, 4, 8)[fcs_flag]
        frame = _Frame()
        while True:
            if pos + 3 > n:
                raise ZstdError("truncated block header")
            h = int.from_bytes(data[pos:pos + 3], "little")
            pos += 3
            last, kind, size = h & 1, (h >> 1) & 3, h >> 3
            if kind == 0:
                if pos + size > n:
                    raise ZstdError("truncated raw block")
                frame.out += data[pos:pos + size]
                pos += size
            elif kind == 1:
                frame.out += bytes([data[pos]]) * size
                pos += 1
            elif kind == 2:
                if pos + size > n:
                    raise ZstdError("truncated compressed block")
                frame.block(bytes(data[pos:pos + size]))
                pos += size
            else:
                raise ZstdError("reserved block type")
            if len(result) + len(frame.out) > max_output:
                raise ZstdError("output too large")
            if last:
                break
        if checksum:
            pos += 4
        result += frame.out
    return bytes(result)
