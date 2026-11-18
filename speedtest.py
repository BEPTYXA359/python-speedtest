#!/usr/bin/env python3
"""speedtest.py — замер скорости скачивания.

Выполняет N последовательных HTTP-запросов к заданному URL, дожидается
полного ответа на каждый и печатает время, объём скачанных данных и
скорость скачивания. Только стандартная библиотека, Python 3.8+.

Примеры:
    python3 speedtest.py
    python3 speedtest.py -n 3 https://example.com/big-file.bin
"""

import argparse
import ipaddress
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple

#: Некоторые CDN отклоняют запросы с дефолтным User-Agent питона.
USER_AGENT = "speedtest.py/1.0"

#: Тело ответа читается блоками этого размера и сразу отбрасывается.
CHUNK_SIZE = 64 * 1024  # 64 КиБ

#: Мегабайт считаем десятичным (10^6 байт), как принято в замерах скорости.
MB = 1_000_000

DEFAULT_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30.0
DEFAULT_SIZE_MB = 25.0

#: Единицы скорости: сколько байт/с в одной единице (десятичные SI,
#: 1 байт = 8 бит, 1 МБ = 10^6 байт, 1 Мбит = 10^6 бит).
UNIT_FACTORS = {
    "bps": 1,          # байт в секунду — сырая база
    "kbs": 1_000,      # КБ/с = 10^3 байт/с
    "mbs": 1_000_000,  # МБ/с = 10^6 байт/с
    "mbps": 125_000,   # Мбит/с = 10^6 бит/с = 125 000 байт/с
}

#: Русские подписи единиц для итогового блока.
UNIT_LABELS = {
    "kbs": "КБ/с",
    "mbs": "МБ/с",
    "mbps": "Мбит/с",
}


class SpeedtestError(Exception):
    """Ошибка замера с сообщением, понятным пользователю."""


def _thousands(value: int) -> str:
    """Форматирует целое с пробелами-разделителями тысяч: 250 000 000."""
    return f"{value:,}".replace(",", " ")


# --- Чистые функции без побочных эффектов ---

def average(times) -> float:
    """Среднее арифметическое по списку; 0.0 для пустого списка."""
    return sum(times) / len(times) if times else 0.0


def convert_speed(value: float, source: str, target: str) -> float:
    """Переводит значение скорости между единицами bps/kbs/mbs/mbps."""
    if source not in UNIT_FACTORS or target not in UNIT_FACTORS:
        raise ValueError(f"неизвестная единица скорости: {source!r} или {target!r}")
    if source == target:
        return value
    bytes_per_sec = value * UNIT_FACTORS[source]
    return bytes_per_sec / UNIT_FACTORS[target]


def speed(nbytes: int, seconds: float, unit: str = "mbs") -> float:
    """Скорость скачивания (байты, секунды) → значение в единице unit."""
    if seconds <= 0:
        return 0.0
    return convert_speed(nbytes / seconds, "bps", unit)


def with_size(url: str, size_mb: float) -> str:
    """Заменяет или добавляет query-параметр bytes=size_mb * 10^6.

    Актуально для Cloudflare-адреса; чужой сервер лишний параметр
    проигнорирует.
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    query["bytes"] = [str(int(round(size_mb * MB)))]
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query, doseq=True))
    )


def _validate_url(url: str) -> None:
    """Проверяет URL перед запросом.

    Разрешены только схемы http и https; хост не должен указывать на
    localhost, петлевой, приватный, link-local или иной зарезервированный
    адрес. Бросает ValueError с описанием проблемы.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"некорректный URL {url!r}: схема должна быть http:// или https://"
        )
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"некорректный URL {url!r}: не указан хост")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"некорректный URL {url!r}: недопустимый порт") from None
    try:
        addrinfos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"не удалось разрешить домен {hostname!r}: {exc}") from exc
    for addrinfo in addrinfos:
        raw_ip = addrinfo[4][0].split("%", 1)[0]  # отбрасываем zone id у IPv6
        ip = ipaddress.ip_address(raw_ip)
        if (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
            raise ValueError(
                f"хост {hostname!r} ({ip}) указывает на локальный, приватный "
                f"или зарезервированный адрес — запрос запрещён"
            )


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Автоматически следует за редиректами, проверяя каждый новый адрес."""

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)


def download(url: str, timeout: float) -> Tuple[int, float]:
    """Скачивает ``url`` и возвращает пару (число байт, секунды).

    - Тело читается блоками по CHUNK_SIZE байт и не копится в памяти целиком.
    - Заголовок Accept-Encoding не отправляется, поэтому счётчик байт —
      это данные, реально пришедшие по сети без сжатия.
    - Время замеряется time.perf_counter() от создания запроса до конца
      тела ответа; время автоматических редиректов входит в замер.
    """
    _validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_ValidatingRedirectHandler)
    start = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            if status is None or not 200 <= status < 300:
                raise SpeedtestError(f"HTTP {status} для {url}")
            downloaded = 0
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                downloaded += len(chunk)
    finally:
        elapsed = time.perf_counter() - start
    return downloaded, elapsed


def _run(url: str, count: int, timeout: float, unit: str = "mbs") -> None:
    """Выполняет замер: построчный прогресс и итоговый блок."""
    _validate_url(url)  # быстрый отказ до печати шапки
    print(f"URL:       {url}")
    print(f"Запросов:  {count}, таймаут сетевых операций: {timeout:g} с")
    print()
    index_width = len(str(count))
    total_bytes = 0
    request_times = []
    for index in range(1, count + 1):
        try:
            nbytes, seconds = download(url, timeout)
        except urllib.error.HTTPError as exc:
            raise SpeedtestError(
                f"HTTP {exc.code} ({exc.reason}) для {url}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SpeedtestError(f"нет доступа к {url}: {exc.reason}") from exc
        total_bytes += nbytes
        request_times.append(seconds)
        print(f"запрос {index:>{index_width}}/{count}: "
              f"{seconds:.3f} с, {nbytes / MB:.2f} МБ")
    total_seconds = sum(request_times)
    # Дубль в скобках: Мбит/с, если она не основная; иначе МБ/с.
    extra_unit = "mbps" if unit != "mbps" else "mbs"
    print()
    print("Итог:")
    print(f"  {'запросов:':<15} {count}")
    print(f"  {'всего скачано:':<15} "
          f"{_thousands(total_bytes)} байт ({total_bytes / MB:.2f} МБ)")
    print(f"  {'общее время:':<15} {total_seconds:.3f} с")
    print(f"  {'среднее время:':<15} {average(request_times):.3f} с")
    print(f"  {'скорость:':<15} "
          f"{speed(total_bytes, total_seconds, unit):.2f} {UNIT_LABELS[unit]} "
          f"({speed(total_bytes, total_seconds, extra_unit):.2f} "
          f"{UNIT_LABELS[extra_unit]})")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speedtest.py",
        description="Замер скорости скачивания: N последовательных запросов "
                    "к URL, отчёт по времени и объёму.",
    )
    parser.add_argument(
        "url", nargs="?", default=DEFAULT_URL,
        help="адрес для замера, только http/https "
             f"(по умолчанию {DEFAULT_URL})",
    )
    parser.add_argument(
        "-n", "--requests", type=int, default=DEFAULT_REQUESTS, metavar="N",
        help=f"число последовательных запросов, >= 1 "
             f"(по умолчанию {DEFAULT_REQUESTS})",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="СЕК",
        help=f"таймаут сетевых операций запроса в секундах "
             f"(по умолчанию {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--size", type=float, default=DEFAULT_SIZE_MB, metavar="МБ",
        help=f"размер одного скачивания в МБ: подменяет query-параметр "
             f"bytes в URL (по умолчанию {DEFAULT_SIZE_MB:g})",
    )
    parser.add_argument(
        "--unit", choices=("mbs", "mbps", "kbs"), default="mbs",
        help="единица скорости в итоге: mbs — МБ/с (по умолчанию), "
             "mbps — Мбит/с, kbs — КБ/с",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.requests < 1:
        parser.error("значение -n/--requests должно быть не меньше 1")
    if args.timeout <= 0:
        parser.error("значение --timeout должно быть больше 0")
    if args.size < 1:
        parser.error("значение --size должно быть не меньше 1")
    url = with_size(args.url, args.size)
    try:
        _run(url, args.requests, args.timeout, args.unit)
    except KeyboardInterrupt:
        print("Прервано пользователем (Ctrl+C)", file=sys.stderr)
        return 130
    except (SpeedtestError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
