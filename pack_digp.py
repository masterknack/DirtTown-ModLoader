#!/usr/bin/env python3
"""Build DIGP v1 archives from a directory or ZIP. Python 3.8+."""
import argparse
import os
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from contextlib import ExitStack

KEY = bytes.fromhex('df022f76cac10a0a53bec444b6d5165d27bff9685ee55d3fecd6f75a13d27a8a')
TABLES = [bytes(i ^ k for i in range(256)) for k in KEY]

def encode(data, offset):
    out = bytearray(data)
    for j in range(32):
        out[j::32] = out[j::32].translate(TABLES[(offset+j-4)%32])
    return out

def write_payload(source, destination, offset, compress, level=6):
    """Stream one payload through optional raw DEFLATE and DIGP's XOR layer."""
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15) if compress else None
    plain_size = stored_size = 0

    def write_chunk(chunk):
        nonlocal stored_size
        if not chunk:
            return
        encrypted = encode(chunk, offset + stored_size)
        destination.write(encrypted)
        stored_size += len(encrypted)

    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        plain_size += len(chunk)
        write_chunk(compressor.compress(chunk) if compressor else chunk)
    if compressor:
        write_chunk(compressor.flush())
    return plain_size, stored_size

def validate(name):
    p = PurePosixPath(name)
    b = name.encode('utf-8')
    forbidden = ('\\', ':', '\x00', '<', '>', '"', '|', '?', '*')
    reserved = {'CON','PRN','AUX','NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}
    if (not b or len(b)>65535 or p.is_absolute() or p.as_posix()!=name or name=='.' or '..' in p.parts
            or any(ord(c)<32 or c in forbidden for c in name)
            or any(part.endswith((' ', '.')) or part.split('.')[0].upper() in reserved for part in name.split('/'))):
        raise ValueError('Invalid archive path: '+name)
    return b

def build(source, output, level=6, force=False, exclude=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    excluded = {PurePosixPath(name).as_posix().casefold() for name in (exclude or ())}
    if source==output or (source.is_dir() and source in output.parents):
        raise ValueError('Output must be outside input directory and differ from input')
    if output.exists() and not force:
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if source.is_dir():
            items = []
            for path in source.rglob('*'):
                if not path.is_file() or path.is_symlink(): continue
                name = path.relative_to(source).as_posix()
                if name.casefold() not in excluded: items.append((name, path))
            open_item = lambda p: p.open('rb')
        else:
            archive = stack.enter_context(zipfile.ZipFile(source))
            items = [(i.filename,i) for i in archive.infolist() if not i.is_dir() and i.filename.casefold() not in excluded]
            open_item = archive.open
        items.sort(key=lambda x:x[0]); seen=set()
        for name,_ in items:
            validate(name)
            folded = name.casefold()
            if folded in seen: raise ValueError('Duplicate or case-colliding path: '+name)
            seen.add(folded)
        index=bytearray(struct.pack('<I',len(items)))
        tmp=None
        try:
            with tempfile.NamedTemporaryFile(mode='w+b',dir=output.parent,delete=False) as f:
                tmp=Path(f.name)
                f.write(b'DIGP'+bytes(20))
                for name,item in items:
                    f.write(bytes((-f.tell())%32)); pos=f.tell()
                    flag=int(Path(name).suffix.lower() not in ('.png','.wav'))
                    with open_item(item) as data:
                        plain, stored = write_payload(data, f, pos, bool(flag), level)
                    b=validate(name)
                    index.extend(struct.pack('<H',len(b))+b+struct.pack('<BQQQ',flag,pos,plain,stored))
                off=f.tell(); f.write(encode(index,off)); f.seek(4)
                f.write(encode(struct.pack('<IQQ',1,off,len(index)),4))
            if output.exists() and not force: raise FileExistsError(output)
            os.replace(tmp,output)
        finally:
            if tmp is not None and tmp.exists(): tmp.unlink()
    print(f'Packed {len(items)} files into {output} ({output.stat().st_size:,} bytes)')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=Path,help='Input directory or ZIP')
    p.add_argument('output',type=Path,help='Output .pack')
    p.add_argument('--level',type=int,choices=range(1,10),default=6)
    p.add_argument('--force',action='store_true',help='Replace existing output')
    a=p.parse_args()
    try: build(a.source,a.output,a.level,a.force)
    except (OSError,ValueError,zipfile.BadZipFile) as e: p.exit(1,f'Error: {e}\n')

if __name__=='__main__': main()
