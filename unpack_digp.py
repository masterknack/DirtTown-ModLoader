import argparse
import struct
import zlib
from pathlib import Path
import zipfile
from pack_digp import validate

KEY = bytes.fromhex('df022f76cac10a0a53bec444b6d5165d27bff9685ee55d3fecd6f75a13d27a8a')
DECODE_TABLES = tuple(bytes(i ^ KEY[k] for i in range(256)) for k in range(32))
CHUNK_SIZE = 1024 * 1024

def decode(data, offset):
    # XOR phase is relative to the four-byte magic.
    out = bytearray(data)
    phase = (offset - 4) % 32
    for j in range(32):
        out[j::32] = out[j::32].translate(DECODE_TABLES[(phase+j)%32])
    return bytes(out)

def decoded_chunks(stream, pos, stored, chunk_size=CHUNK_SIZE):
    """Read and XOR-decode a payload without buffering the whole resource."""
    if chunk_size < 1 or pos < 0 or stored < 0:
        raise ValueError('Invalid payload range or chunk size')
    stream.seek(pos)
    consumed = 0
    while consumed < stored:
        size = min(chunk_size, stored - consumed)
        chunk = stream.read(size)
        if len(chunk) != size:
            raise ValueError('Truncated payload')
        yield decode(chunk, pos + consumed)
        consumed += size

def payload_chunks(stream, flag, pos, plain, stored, chunk_size=CHUNK_SIZE):
    """Yield validated decoded bytes, bounding decompression memory by chunk_size."""
    if flag not in (0, 1) or plain < 0 or stored < 0 or chunk_size < 1:
        raise ValueError('Invalid payload metadata')
    decompressor = zlib.decompressobj(-15) if flag else None
    produced = 0

    def emit(data):
        nonlocal produced
        produced += len(data)
        if produced > plain:
            raise ValueError('Size mismatch: decompressed payload exceeds declared size')
        if data:
            yield data

    for decoded in decoded_chunks(stream, pos, stored, chunk_size):
        if decompressor is None:
            yield from emit(decoded)
            continue

        pending = decoded
        while True:
            try:
                output = decompressor.decompress(pending, chunk_size)
            except zlib.error as exc:
                raise ValueError(f'Invalid DEFLATE payload: {exc}') from exc
            pending = decompressor.unconsumed_tail
            if decompressor.unused_data:
                raise ValueError('Invalid DEFLATE: trailing data')
            yield from emit(output)
            if pending:
                continue
            # max_length may leave output buffered internally even with no input tail.
            if len(output) == chunk_size:
                pending = b''
                continue
            break

    if decompressor is not None and (not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail):
        raise ValueError('Invalid DEFLATE stream')
    if produced != plain:
        raise ValueError('Size mismatch: payload differs from declared size')

def entries(f):
    f.seek(0,2); total=f.tell(); f.seek(0)
    h=f.read(24)
    if len(h)!=24 or h[:4]!=b'DIGP': raise ValueError('Invalid magic/header')
    ver,off,size=struct.unpack('<IQQ',decode(h[4:],4))
    if ver!=1 or off<24 or size<4 or off+size>total: raise ValueError('Invalid index')
    f.seek(off); b=decode(f.read(size),off)
    count,=struct.unpack_from('<I',b); p=4; seen=set()
    for _ in range(count):
        n,=struct.unpack_from('<H',b,p); p+=2
        name=b[p:p+n].decode('utf-8'); p+=n
        flag,pos,plain,stored=struct.unpack_from('<BQQQ',b,p); p+=25
        validate(name)
        if name in seen: raise ValueError('Unsafe or duplicate path: '+name)
        seen.add(name)
        if flag not in (0,1) or pos<24 or pos+stored>off or (flag==0 and plain!=stored): raise ValueError('Invalid entry: '+name)
        yield name,flag,pos,plain,stored
    if p!=len(b): raise ValueError('Index size mismatch')

def main():
    ap=argparse.ArgumentParser(description='DIGP v1 archive extractor')
    ap.add_argument('archive',type=Path); ap.add_argument('output',type=Path,nargs='?',default=Path('extracted.zip')); ap.add_argument('--list',action='store_true'); args=ap.parse_args()
    with args.archive.open('rb') as f:
        items=list(entries(f))
        if args.list:
            for name,flag,pos,plain,stored in items: print(f'{plain:12} {stored:12} {flag} {name}')
            return
        if args.output.resolve()==args.archive.resolve(): raise ValueError('Output must differ from input')
        with zipfile.ZipFile(args.output,'x',compression=zipfile.ZIP_STORED) as out:
            for name,flag,pos,plain,stored in items:
                with out.open(name, 'w') as target:
                    for chunk in payload_chunks(f,flag,pos,plain,stored):
                        target.write(chunk)
        print(f'Extracted {len(items)} files to {args.output}')
if __name__=='__main__': main()
