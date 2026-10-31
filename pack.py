# -*- coding: utf-8 -*-
"""把工程打包成可直接发给朋友的 zip。

素材库有 2GB 多，PowerShell 的 Compress-Archive 会卡住/失败，
所以用 zipfile 流式压缩（JPEG/PNG 本身已压缩，store 模式即可，快很多）。
"""
import io
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "dist")
STAGE = os.path.join(OUT, "Atlas-Studio")
ZIP = os.path.join(OUT, "Atlas-Studio.zip")

# 不需要打包的东西
SKIP_DIRS = {"work", "__pycache__", ".git", "dist", "versions"}
SKIP_EXT = (".pyc", ".pyo", ".log")


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n


def clean_config():
    """清掉指向原机器的路径，让对方重新自动扫描。"""
    for rel in ("app/config.json", "skill/flux2-klein-portrait/config.json"):
        p = os.path.join(STAGE, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        d["comfy_root"] = ""
        d["python"] = ""
        try:
            io.open(p, "w", encoding="utf-8").write(
                json.dumps(d, ensure_ascii=False, indent=2))
            print("   cleaned %s" % rel)
        except OSError:
            pass


def main():
    if not os.path.isdir(STAGE):
        print("stage missing: %s" % STAGE)
        return 1

    clean_config()

    if os.path.exists(ZIP):
        os.remove(ZIP)
    print("   compressing ...")
    n = 0
    total = 0
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        prefix = os.path.basename(STAGE)      # zip 内保留 Atlas-Studio/ 这一层
        for base, dirs, files in os.walk(STAGE):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(SKIP_EXT):
                    continue
                full = os.path.join(base, f)
                rel = os.path.relpath(full, STAGE).replace(os.sep, "/")
                arc = prefix + "/" + rel
                try:
                    z.write(full, arc)
                    n += 1
                    total += os.path.getsize(full)
                except OSError:
                    pass
            if n % 500 == 0 and n:
                print("      %d files, %s" % (n, human(total)))

    print("   OK: %d files, %s -> %s" % (n, human(total), ZIP))
    print("   zip size: %s" % human(os.path.getsize(ZIP)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
