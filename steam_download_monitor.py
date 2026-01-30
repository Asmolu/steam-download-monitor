#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import platform
from pathlib import Path

PAUSE_EPS_BYTES_PER_MIN = 256 * 1024  # <256KB/min считаем простоем/паузой


def human_mb_per_s(bytes_per_s: float) -> str:
    return f"{bytes_per_s / (1024 * 1024):.2f} MB/s"


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


def detect_pause_from_log(steam_root: Path, appid: str) -> bool | None:
    log_path = steam_root / "logs" / "content_log.txt"
    t = tail_text(log_path)
    if not t:
        return None
    low = t.lower()
    if "pause" in low and appid in low:
        return True
    return None


def main():
    if platform.system().lower() != "windows":
        print("Этот вариант заточен под Windows (реестр + типовые пути).")
        sys.exit(1)

    steam_root = get_steam_path_windows()
    if not steam_root:
        print("Steam не найден (не удалось определить путь установки).")
        sys.exit(1)

    libraries = get_steam_libraries(steam_root)

    active = pick_active_download(libraries)
    if not active:
        print(f"Steam найден: {steam_root}")
        print(f"Библиотеки: {', '.join(map(str, libraries))}")
        print("Активных загрузок не обнаружено (steamapps/downloading пуст во всех библиотеках).")
        sys.exit(0)

    lib_path, appid = active
    game_name = read_game_name_from_manifest(lib_path, appid) or f"AppID {appid}"
    dl_dir = lib_path / "steamapps" / "downloading" / appid

    print(f"Steam root: {steam_root}")
    print(f"Steam libraries: {', '.join(map(str, libraries))}")
    print(f"Загрузка: {game_name}")
    print(f"Library: {lib_path}")
    print(f"Downloading dir: {dl_dir}")
    print("Отчёт: 1 раз в минуту, 5 минут\n")

    prev = dir_size_bytes(dl_dir)

    for minute in range(1, 6):
        time.sleep(60)
        cur = dir_size_bytes(dl_dir)
        delta = max(0, cur - prev)
        prev = cur

        bytes_per_s = delta / 60.0
        paused_by_delta = delta < PAUSE_EPS_BYTES_PER_MIN
        paused_by_log = detect_pause_from_log(steam_root, appid)
        paused = paused_by_delta if paused_by_log is None else paused_by_log

        status = "PAUSED/IDLE" if paused else "DOWNLOADING"
        print(
            f"[{minute}/5] {game_name} | {status} | "
            f"speed: {human_mb_per_s(bytes_per_s)} | +{delta / (1024*1024):.2f} MB/min"
        )

    print("\nГотово.")


if __name__ == "__main__":
    main()