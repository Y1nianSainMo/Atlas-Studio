# -*- coding: utf-8 -*-
"""多线程分段下载（HTTP Range），用于 hf-mirror 慢速场景"""
import os, sys, json, time, urllib.request, threading

def head_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get("Content-Length", 0)), r.headers.get("Accept-Ranges", "")

def fetch_range(url, start, end, buf, idx, prog, lock):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    got = 0
    with urllib.request.urlopen(req, timeout=120) as r:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            buf[idx].extend(chunk)
            got += len(chunk)
            with lock:
                prog[0] += len(chunk)

def download(url, dst, parts=10, chunk_report=5):
    total, ar = head_size(url)
    if total <= 0:
        raise RuntimeError("no content-length")
    print(f"total={total/1024/1024:.1f} MB ranges={ar}")
    base = dst + ".part"
    # 断点续传：已有部件则复用大小
    start_off = os.path.getsize(base) if os.path.exists(base) else 0
    remaining = total - start_off
    seg = remaining // parts

    buf, spans = [], []
    for i in range(parts):
        s = start_off + i * seg
        e = total - 1 if i == parts - 1 else start_off + (i + 1) * seg - 1
        spans.append((s, e))
        buf.append(bytearray())

    prog, lock = [0], threading.Lock()
    ths = []
    t0 = time.time()
    for i, (s, e) in enumerate(spans):
        t = threading.Thread(target=fetch_range, args=(url, s, e, buf, i, prog, lock), daemon=True)
        t.start(); ths.append(t)

    while any(t.is_alive() for t in ths):
        time.sleep(chunk_report)
        el = time.time() - t0
        print(f"  {prog[0]/1024/1024:6.1f} MB  {prog[0]/1024/el:6.1f} KB/s  {el:5.0f}s", flush=True)

    for t in ths:
        t.join()
    with open(base, "ab") as f:
        for b in buf:
            f.write(b)
    os.replace(base, dst)
    print("saved", dst, os.path.getsize(dst))

if __name__ == "__main__":
    download(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 10)
