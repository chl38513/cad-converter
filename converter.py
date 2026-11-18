# -*- coding: utf-8 -*-
"""DXF <-> DWG 转换引擎层。

内置 GNU LibreDWG（Windows 二进制），并支持自动探测 ODA File Converter。
所有转换都在纯 ASCII 的临时目录里进行，再把结果原子写回用户指定的真实路径，
以规避部分 CAD 工具对中文/空格路径的兼容问题。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from typing import Any

ROOT = os.path.dirname(os.path.abspath(__file__))
LIBREDWG_DIR = os.path.join(ROOT, "engine", "libredwg")
DWG2DXF = os.path.join(LIBREDWG_DIR, "dwg2dxf.exe")
DXF2DWG = os.path.join(LIBREDWG_DIR, "dxf2dwg.exe")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DWG_VERSIONS = ["r2000", "r12", "r14", "r2004"]
DXF_VERSIONS = ["auto", "r2000", "r12", "r14", "r2004", "r2007", "r2010", "r2013", "r2018"]
ODA_VERSIONS = ["ACAD2018", "ACAD2013", "ACAD2010", "ACAD2007", "ACAD2004", "ACAD2000"]

ACADVER_LABEL = {
    "AC1012": "R13", "AC1014": "R14", "AC1015": "2000", "AC1018": "2004",
    "AC1021": "2007", "AC1024": "2010", "AC1027": "2013", "AC1032": "2018",
    "AC1035": "2024", "AC1009": "R12", "AC1006": "R10",
}

# DWG 文件头 → LibreDWG 的 --as 输出版本
DWG_HEADER_TO_DXF = {
    "AC1006": "r12", "AC1009": "r12", "AC1012": "r14", "AC1014": "r14",
    "AC1015": "r2000", "AC1018": "r2004", "AC1021": "r2007",
    "AC1024": "r2010", "AC1027": "r2013", "AC1032": "r2018", "AC1035": "r2018",
}
# LibreDWG 以这些版本输出 DXF 时，会写出空的 BLOCK 定义，
# 导致尺寸标注的线/箭头/文字以及匿名块图形全部消失。
LOSSY_DXF_VERSIONS = {"r12", "r14", "r2000"}


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def find_oda(explicit: str = "") -> str | None:
    """查找 ODA File Converter 可执行文件。"""
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    names = ("ODAFileConverter.exe", "TeighaFileConverter.exe")
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    for root in roots:
        if not root:
            continue
        for pattern in (os.path.join(root, "ODA", "*"), os.path.join(root, "*ODA*"),
                        os.path.join(root, "*Teigha*")):
            for folder in glob.glob(pattern):
                if os.path.isdir(folder):
                    for n in names:
                        p = os.path.join(folder, n)
                        if os.path.isfile(p):
                            return p
                elif os.path.isfile(folder):
                    return folder
    for n in ("ODAFileConverter", "TeighaFileConverter"):
        p = _which(n)
        if p:
            return p
    return None


def engine_status(explicit_oda: str = "") -> dict[str, Any]:
    oda = find_oda(explicit_oda)
    return {
        "libredwg": {
            "available": os.path.isfile(DWG2DXF) and os.path.isfile(DXF2DWG),
            "path": LIBREDWG_DIR,
            "label": "LibreDWG 0.14（内置）",
        },
        "oda": {
            "available": bool(oda),
            "path": oda or "",
            "label": "ODA File Converter（可选，保真度更高）",
        },
    }


def probe_version(path: str) -> str:
    """读取文件的 CAD 版本标识。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            if ext == ".dwg":
                head = f.read(6).decode("ascii", "ignore")
                return ACADVER_LABEL.get(head, head or "未知")
            head = f.read(65536).decode("utf-8", "ignore").replace("\r", "")
            lines = head.split("\n")
            for i, line in enumerate(lines):
                if line.strip() == "$ACADVER" and i + 2 < len(lines):
                    code = lines[i + 2].strip()
                    return ACADVER_LABEL.get(code, code or "未知")
    except Exception:
        pass
    return "未知"


def resolve_dxf_version(src: str, requested: str) -> tuple[str, str]:
    """把「auto」解析成与源 DWG 匹配的输出版本，返回 (版本, 日志说明)。"""
    if requested != "auto":
        return requested, ""
    head = ""
    try:
        with open(src, "rb") as f:
            head = f.read(6).decode("ascii", "ignore").strip()
    except OSError:
        pass
    version = DWG_HEADER_TO_DXF.get(head)
    if version:
        label = ACADVER_LABEL.get(head, head)
        return version, "源图纸为 R%s，自动按 %s 输出 DXF（保留块与标注）" % (
            label, version.upper())
    return "r2010", "未能识别源 DWG 版本，按 R2010 输出（保真优先）"


def dxf_block_stats(path: str) -> tuple[int, int]:
    """统计 DXF 的 (BLOCK 记录数, 块内图元数)。

    *Model_Space / *Paper_Space 是任何图纸都有的两个空块，因此
    「块数 > 2 且块内图元数 = 0」即可判定块定义被转换引擎清空。
    """
    try:
        with open(path, "rb") as f:
            text = f.read().decode("latin-1")
    except OSError:
        return (0, 0)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pairs = [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]
    in_blocks = False
    nblocks = ninner = 0
    for k, (code, value) in enumerate(pairs):
        if code == "0" and value.strip().upper() == "SECTION":
            in_blocks = (k + 1 < len(pairs) and pairs[k + 1][0] == "2"
                         and pairs[k + 1][1].strip().upper() == "BLOCKS")
            continue
        if not in_blocks:
            continue
        if code == "0" and value.strip().upper() == "ENDSEC":
            break
        if code == "0":
            kind = value.strip().upper()
            if kind == "BLOCK":
                nblocks += 1
            elif kind != "ENDBLK":
                ninner += 1
    return nblocks, ninner


def _ascii_temp_parent() -> str:
    base = tempfile.gettempdir()
    if base.isascii():
        return base
    drive = os.environ.get("SystemDrive", "C:") + os.sep
    for cand in (os.path.join(drive, "Temp"), os.path.join(drive, "Windows", "Temp")):
        try:
            os.makedirs(cand, exist_ok=True)
            if os.access(cand, os.W_OK) and cand.isascii():
                return cand
        except Exception:
            continue
    return base


def _run(cmd: list[str], cwd: str, timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = (p.stdout or b"") + b"\n" + (p.stderr or b"")
        text = raw.decode("utf-8", "replace")
        if text.count("\ufffd") > len(text) // 20:
            text = raw.decode("mbcs", "replace")
        return p.returncode, text
    except subprocess.TimeoutExpired:
        return -1, "转换超时（超过 %d 秒）" % timeout
    except Exception as exc:
        return -1, "无法启动转换程序：%s" % exc


def repair_dxf(path: str) -> list[str]:
    """修复 LibreDWG 输出 DXF 的常见问题：非法 handle、缺失 EOF、换行符。

    使用 latin-1 逐字节读写，保证不破坏图纸里的中文/ANSI 文字编码。
    """
    notes: list[str] = []
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("latin-1")
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and lines[-1].strip() == "":
            lines.pop()
        if len(lines) % 2:
            lines.append("")
        has_eof = (len(lines) >= 2 and lines[-2].strip() == "0"
                   and lines[-1].strip().upper() == "EOF")
        if not has_eof and raw:
            lines += ["0", "EOF"]
            notes.append("补全缺失的 EOF 结束标记")

        pairs = [(lines[i].strip(), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]
        max_handle = 0
        for code, value in pairs:
            if code == "5":
                try:
                    max_handle = max(max_handle, int(value.strip(), 16))
                except Exception:
                    pass
        nxt = max_handle + 1
        fixed = 0
        for j, (code, value) in enumerate(pairs):
            if code == "5":
                v = value.strip()
                valid = False
                try:
                    valid = int(v, 16) > 0
                except Exception:
                    valid = False
                if not valid:
                    pairs[j] = (code, format(nxt, "X"))
                    nxt += 1
                    fixed += 1
        if fixed:
            notes.append("修复 %d 个非法图元句柄（handle=0）" % fixed)
        out_lines: list[str] = []
        for code, value in pairs:
            out_lines.extend([code, value])
        with open(path, "wb") as f:
            f.write(("\r\n".join(out_lines) + "\r\n").encode("latin-1"))
    except Exception as exc:
        notes.append("规范化跳过：%s" % exc)
    return notes


def _valid_output(path: str, ext: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) < 64:
        return False
    if ext == ".dwg":
        with open(path, "rb") as f:
            return f.read(4).startswith(b"AC")
    with open(path, "rb") as f:
        data = f.read(4096).upper()
    return b"SECTION" in data or b"ENTITIES" in data


def _libredwg_convert(src_tmp: str, out_tmp: str, direction: str,
                      dwg_version: str, dxf_version: str,
                      tmpdir: str) -> tuple[bool, str, list[str]]:
    logs: list[str] = []
    if direction == "dwg2dxf":
        exe, version = DWG2DXF, dxf_version
        if version == "auto":
            version, note = resolve_dxf_version(src_tmp, "auto")
            if note:
                logs.append(note)
    else:
        exe, version = DXF2DWG, dwg_version

    def attempt(args: list[str]) -> tuple[bool, str]:
        rc, text = _run([exe, "-y"] + args, cwd=tmpdir)
        keep = [l.strip() for l in text.splitlines()
                if any(k in l for k in ("ERROR", "Failed", "Reading ", "Writing ", "too small"))]
        logs.extend(keep[-12:])
        return _valid_output(out_tmp, os.path.splitext(out_tmp)[1]), text[-500:]

    ok, tail = attempt(["--as", version, "-o", out_tmp, src_tmp])

    # 低版本 DXF 输出会把块定义写成空块，尺寸标注的线/箭头/文字和
    # 匿名块图形会随之消失。检测到这种情况就自动按源版本重出一次。
    if ok and direction == "dwg2dxf" and version in LOSSY_DXF_VERSIONS:
        nblocks, ninner = dxf_block_stats(out_tmp)
        if nblocks > 2 and ninner == 0:
            alt, _ = resolve_dxf_version(src_tmp, "auto")
            if alt != version:
                logs.append("%s 输出清空了 %d 个块定义（标注/块图形会消失），自动改用 %s 重出"
                            % (version.upper(), nblocks, alt.upper()))
                try:
                    os.remove(out_tmp)
                except OSError:
                    pass
                ok2, tail2 = attempt(["--as", alt, "-o", out_tmp, src_tmp])
                if ok2:
                    ok, tail = ok2, tail2
                    nb2, ni2 = dxf_block_stats(out_tmp)
                    logs.append("已按 %s 重出：保留 %d 个块内图元" % (alt.upper(), ni2))
                    version = alt
                else:
                    logs.append("自动重出失败，保留原输出")

    if not ok and direction == "dwg2dxf":
        # 完整结构输出失败时，退回最小 DXF（保留几何，丢失图层/样式）
        logs.append("完整结构输出失败，改用兼容模式（-m）重试")
        try:
            os.remove(out_tmp)
        except OSError:
            pass
        ok, tail = attempt(["-m", "-y", "-o", out_tmp, src_tmp])
        if ok:
            logs.append("兼容模式成功：仅保留图元，图层/线型等信息可能丢失")
    return ok, tail, logs


def _oda_convert(oda: str, src_tmp: str, out_tmp: str, direction: str,
                 version: str, tmpdir: str) -> tuple[bool, str, list[str]]:
    in_dir = os.path.join(tmpdir, "odain")
    out_dir = os.path.join(tmpdir, "odaout")
    os.makedirs(in_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2(src_tmp, os.path.join(in_dir, os.path.basename(src_tmp)))
    fmt = "DXF" if direction == "dwg2dxf" else "DWG"
    cmd = [oda, in_dir, out_dir, version, fmt, "0", "1", os.path.basename(src_tmp)]
    rc, text = _run(cmd, cwd=tmpdir, timeout=240)
    expect = os.path.join(out_dir, os.path.splitext(os.path.basename(src_tmp))[0] + "." + fmt.lower())
    if not os.path.isfile(expect):
        found = glob.glob(os.path.join(out_dir, "*." + fmt.lower()))
        expect = found[0] if found else expect
    if os.path.isfile(expect):
        shutil.copy2(expect, out_tmp)
    ok = _valid_output(out_tmp, os.path.splitext(out_tmp)[1])
    logs = [l.strip() for l in text.splitlines() if l.strip()][-8:]
    if not ok:
        logs.append("ODA 未产生有效输出（rc=%s）" % rc)
    return ok, text[-500:], logs


def convert_one(src: str, dst: str, options: dict[str, Any]) -> dict[str, Any]:
    """转换单个文件，返回结构化结果。dst 为完整目标路径。"""
    result: dict[str, Any] = {"ok": False, "input": src, "output": dst,
                              "engine": "", "logs": [], "size": 0}
    if not os.path.isfile(src):
        result["error"] = "源文件不存在"
        return result
    ext = os.path.splitext(src)[1].lower()
    if ext not in (".dxf", ".dwg"):
        result["error"] = "仅支持 .dxf / .dwg 文件"
        return result
    direction = "dwg2dxf" if ext == ".dwg" else "dxf2dwg"
    out_ext = ".dxf" if ext == ".dwg" else ".dwg"

    on_exists = options.get("onExists", "error")
    if os.path.exists(dst):
        if on_exists == "skip":
            result["skipped"] = True
            result["ok"] = True
            result["logs"].append("目标已存在，已跳过")
            return result
        if on_exists == "rename":
            stem, e = os.path.splitext(dst)
            i = 1
            while os.path.exists(f"{stem}_{i}{e}"):
                i += 1
            dst = f"{stem}_{i}{e}"
            result["output"] = dst
        elif on_exists != "overwrite":
            result["error"] = "目标文件已存在"
            result["exists"] = True
            return result

    status = engine_status(str(options.get("odaPath", "")))
    requested = options.get("engine", "auto")
    chain: list[str] = []
    if requested == "libredwg":
        chain = ["libredwg"] if status["libredwg"]["available"] else []
    elif requested == "oda":
        chain = ["oda"] if status["oda"]["available"] else []
    else:
        if status["oda"]["available"]:
            chain.append("oda")
        if status["libredwg"]["available"]:
            chain.append("libredwg")
    if not chain:
        result["error"] = "没有可用的转换引擎，请在「引擎设置」中指定 ODA File Converter 路径"
        return result

    tmpdir = tempfile.mkdtemp(prefix="cadcv_", dir=_ascii_temp_parent())
    try:
        src_tmp = os.path.join(tmpdir, "input" + ext)
        out_tmp = os.path.join(tmpdir, "output" + out_ext)
        shutil.copy2(src, src_tmp)
        all_logs: list[str] = []
        chosen = ""
        for eng in chain:
            all_logs.append("尝试引擎：%s" % eng)
            if eng == "libredwg":
                ok, tail, logs = _libredwg_convert(
                    src_tmp, out_tmp, direction,
                    str(options.get("dwgVersion", "r2000")),
                    str(options.get("dxfVersion", "auto")), tmpdir)
            else:
                ok, tail, logs = _oda_convert(
                    status["oda"]["path"], src_tmp, out_tmp, direction,
                    str(options.get("odaVersion", "ACAD2018")), tmpdir)
            all_logs.extend(logs)
            if ok:
                chosen = eng
                break
            all_logs.append("该引擎未成功，输出摘要：%s" % tail[-160:].replace("\n", " "))
            try:
                os.remove(out_tmp)
            except OSError:
                pass
        if not chosen:
            result["error"] = "所有可用引擎均转换失败"
            result["logs"] = all_logs
            return result

        if chosen == "libredwg" and direction == "dwg2dxf":
            all_logs.extend("DXF 规范化：" + n for n in repair_dxf(out_tmp))

        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        stage = dst + ".cadcv.tmp"
        shutil.copy2(out_tmp, stage)
        os.replace(stage, dst)
        result.update({
            "ok": True,
            "engine": chosen,
            "size": os.path.getsize(dst),
            "logs": all_logs,
            "version": probe_version(dst),
        })
        return result
    except Exception as exc:
        result["error"] = "写入输出文件失败：%s" % exc
        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def preview_source_dxf(path: str, workdir: str) -> str | None:
    """把 DWG 临时转成精简 DXF 供预览；DXF 直接返回原路径。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dxf":
        return path
    if ext != ".dwg" or not os.path.isfile(DWG2DXF):
        return None
    tmp = os.path.join(workdir, "preview.dxf")
    try:
        os.remove(tmp)
    except OSError:
        pass
    rc, _ = _run([DWG2DXF, "-y", "-m", "-o", tmp, path], cwd=workdir, timeout=60)
    if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 64:
        return tmp
    return None
