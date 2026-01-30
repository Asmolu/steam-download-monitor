#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import platform
from pathlib import Path

PAUSE_EPS_BYTES_PER_MIN = 256 * 1024  # <256KB/min считаем простоем/паузой
LOG_SPEED_EPS_MB_S = 0.10  # <0.1MB/s считаем отсутствием скачивания

def format_speed(bytes_per_s: float) -> str:
    mb_per_s = bytes_per_s / (1024 * 1024)
    mbit_per_s = (bytes_per_s * 8) / 1_000_000
    return f"{mb_per_s:.2f} MB/s ({mbit_per_s:.2f} Mbit/s)"


def dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _, files in os.walk(path):
        for fn in files:
            fp = Path(root) / fn
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def tail_text(path: Path, max_bytes: int = 200_000) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def get_steam_path_windows() -> Path | None:
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            for name in ("SteamPath", "InstallPath"):
                try:
                    val, _ = winreg.QueryValueEx(k, name)
                    p = Path(val)
                    if p.exists():
                        return p
                except OSError:
                    pass
    except Exception:
        pass

    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Steam",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def parse_libraryfolders_vdf(vdf_path: Path) -> list[Path]:
    """
    Упрощённый парсер: Steam VDF — это текст с кавычками.
    В libraryfolders.vdf встречается:
      "path"    "D:\\SteamLibrary"
    или старый формат:
      "1"  "D:\\SteamLibrary"
    Берём все строки, где есть путь и существует папка steamapps.
    """
    libs: list[Path] = []
    if not vdf_path.exists():
        return libs

    try:
        text = vdf_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return libs

    for raw in text.splitlines():
        line = raw.strip()
        if '"' not in line:
            continue

        parts = line.split('"')
        # parts вида: ['', key, '  ', value, ...]
        if len(parts) < 4:
            continue

        key = parts[1].strip().lower()
        value = parts[3].strip()

        if not value:
            continue

        # интересуют либо ключ "path", либо цифровые ключи (старый формат)
        if key == "path" or key.isdigit():
            # Steam пишет с экранированием backslash
            value_norm = value.replace("\\\\", "\\")
            p = Path(value_norm)

            # библиотека валидна, если есть steamapps
            if (p / "steamapps").exists():
                libs.append(p)

    # уникализируем, сохраняя порядок
    seen = set()
    out = []
    for p in libs:
        s = str(p).lower()
        if s not in seen:
            seen.add(s)
            out.append(p)
    return out


def get_steam_libraries(steam_root: Path) -> list[Path]:
    """
    Возвращает список библиотек Steam (включая root).
    """
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    extra = parse_libraryfolders_vdf(vdf)
    for p in extra:
        if p not in libs:
            libs.append(p)
    return libs


def pick_active_download(libraries: list[Path]) -> tuple[Path, str] | None:
    """
    Ищем активную загрузку в любой библиотеке.
    Возвращаем (library_path, appid) по самой “свеже изменяемой” папке downloading/<appid>.
    """
    candidates: list[tuple[float, Path, str]] = []

    for lib in libraries:
        downloading = lib / "steamapps" / "downloading"
        if not downloading.exists():
            continue

        for child in downloading.iterdir():
            if child.is_dir() and child.name.isdigit():
                try:
                    mtime = child.stat().st_mtime
                    candidates.append((mtime, lib, child.name))
                except OSError:
                    pass

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, lib, appid = candidates[0]
    return lib, appid


def read_game_name_from_manifest(library_path: Path, appid: str) -> str | None:
    manifest = library_path / "steamapps" / f"appmanifest_{appid}.acf"
    if not manifest.exists():
        return None

    try:
        text = manifest.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for line in text.splitlines():
        s = line.strip()
        if s.startswith('"name"'):
            parts = s.split('"')
            if len(parts) >= 4:
                name = parts[3].strip()
                if name:
                    return name
    return None


def find_pause_resume_indices(text: str, appid: str) -> tuple[int | None, int | None]:
    pause_markers = ("pause", "paused", "pausing")
    resume_markers = ("resume", "resumed", "unpause", "unpaused")
    context_markers = ("download", "content", "depot", "app")
    pause_idx = None
    resume_idx = None

    for idx, line in enumerate(reversed(text.splitlines())):
        low = line.lower()
        if appid not in low and not any(marker in low for marker in context_markers):
            continue
        if resume_idx is None and any(marker in low for marker in resume_markers):
            resume_idx = idx
        if pause_idx is None and any(marker in low for marker in pause_markers):
            pause_idx = idx
        if pause_idx is not None and resume_idx is not None:
            break

    return pause_idx, resume_idx

def parse_speed_from_line(line: str) -> float | None:
    """
    Пытаемся вытащить скорость из строки лога.
    Возвращает bytes/sec.
    """
    patterns = [
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>kbit|mbit|gbit)/s",
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>kb|mb|gb|kib|mib|gib)/s",
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>kbps|mbps|gbps)",
    ]
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if not match:
            continue
        value = float(match.group("value").replace(",", "."))
        unit = match.group("unit").lower()
        is_bits = "bit" in unit or unit.endswith("bps")
        decimal_base = 1000
        binary_base = 1024
        if unit.startswith("gi"):
            value *= binary_base ** 3
        elif unit.startswith("mi"):
            value *= binary_base ** 2
        elif unit.startswith("ki"):
            value *= binary_base
        elif unit.startswith("g"):
            value *= decimal_base ** 3
        elif unit.startswith("m"):
            value *= decimal_base ** 2
        elif unit.startswith("k"):
            value *= decimal_base
        if is_bits:
            value /= 8
        return value
    return None


def find_speed_in_text(text: str, appid: str) -> tuple[float | None, int | None]:
    for idx, line in enumerate(reversed(text.splitlines())):
        low = line.lower()
        if appid not in low and "download" not in low:
            continue
        speed = parse_speed_from_line(line)
        if speed is not None:
            return speed, idx
    return None, None


def read_log_state(steam_root: Path, appid: str) -> tuple[float | None, int | None, int | None, int | None]:
    """
    Возвращает (speed_bytes_per_s, speed_idx, pause_idx, resume_idx).
    Индексы — это позиция строки в обратном порядке (0 = самая свежая).
    """
    content_log = steam_root / "logs" / "content_log.txt"
    connection_log = steam_root / "logs" / "connection_log.txt"

    content_text = tail_text(content_log)
    connection_text = tail_text(connection_log)

    speed, speed_idx = (None, None)
    if content_text:
        speed, speed_idx = find_speed_in_text(content_text, appid)
    if speed is None and connection_text:
        speed, speed_idx = find_speed_in_text(connection_text, appid)

    pause_idx = resume_idx = None
    if content_text:
        pause_idx, resume_idx = find_pause_resume_indices(content_text, appid)

    return speed, speed_idx, pause_idx, resume_idx


def main():
    if platform.system().lower() != "windows":
        print("Этот вариант заточен под Windows (реестр + типовые пути).")
        sys.exit(1)

    steam_root = get_steam_path_windows()
    if not steam_root:
        print("Steam не найден (не удалось определить путь установки).")
        sys.exit(1)

    libraries = get_steam_libraries(steam_root)


    print(f"Steam root: {steam_root}")
    print(f"Steam libraries: {', '.join(map(str, libraries))}")
    print("Отчёт: 1 раз в минуту, 5 минут\n")

    prev_sizes: dict[str, int] = {}

    for minute in range(1, 6):
        active = pick_active_download(libraries)
        if not active:
            print(f"[{minute}/5] Активных загрузок нет.")
            time.sleep(60)
            continue

        lib_path, appid = active
        game_name = read_game_name_from_manifest(lib_path, appid) or f"AppID {appid}"
        dl_dir = lib_path / "steamapps" / "downloading" / appid

        prev = prev_sizes.get(appid, dir_size_bytes(dl_dir))
        time.sleep(60)
        cur = dir_size_bytes(dl_dir)
        delta = max(0, cur - prev)
        prev_sizes[appid] = cur

        bytes_per_s_disk = delta / 60.0
        bytes_per_s_log, speed_idx, pause_idx, resume_idx = read_log_state(steam_root, appid)
        paused_by_delta = delta < PAUSE_EPS_BYTES_PER_MIN
        pause_decision = None
        if pause_idx is not None and (resume_idx is None or pause_idx < resume_idx):
            pause_decision = True
        elif resume_idx is not None and (pause_idx is None or resume_idx < pause_idx):
            pause_decision = False

        speed_is_recent = (
            bytes_per_s_log is not None
            and pause_decision is not True
            and (pause_idx is None or speed_idx is not None and speed_idx < pause_idx)
            and (resume_idx is None or speed_idx is not None and speed_idx < resume_idx)
            and (bytes_per_s_log / (1024 * 1024)) > LOG_SPEED_EPS_MB_S
        )
        bytes_per_s = bytes_per_s_log if speed_is_recent else bytes_per_s_disk

        if pause_decision is True:
            paused = True
        elif pause_decision is False:
            paused = paused_by_delta and not speed_is_recent
        else:
            paused = paused_by_delta and not speed_is_recent

        status = "PAUSED/IDLE" if paused else "DOWNLOADING"
        print(
            f"[{minute}/5] {game_name} | {status} | "
            f"speed: {format_speed(bytes_per_s)} | +{delta / (1024*1024):.2f} MB/min"
        )

    print("\nГотово.")


if __name__ == "__main__":
    main()