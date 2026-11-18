"""Офлайн-тесты speedtest.py: локальный http.server в потоке, без внешней сети.

Запуск из корня проекта:
    python3 -m unittest discover -s tests -v
"""

import contextlib
import http.server
import io
import threading
import unittest
import unittest.mock
import urllib.error

import speedtest


class _SpeedHandler(http.server.BaseHTTPRequestHandler):
    """/bytes/N → N байт с Content-Length; /stream/N → без Content-Length.

    Остальное — 404.
    """

    def do_GET(self):
        if self.path.startswith("/bytes/"):
            self._serve(int(self.path.rsplit("/", 1)[1]), with_length=True)
        elif self.path.startswith("/stream/"):
            self._serve(int(self.path.rsplit("/", 1)[1]), with_length=False)
        else:
            self.send_error(404)

    def _serve(self, n, with_length):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if with_length:
            self.send_header("Content-Length", str(n))
        self.end_headers()
        remaining = n
        while remaining:
            chunk = b"x" * min(8192, remaining)
            self.wfile.write(chunk)
            remaining -= len(chunk)

    def log_message(self, format, *args):  # сигнатура базового класса
        pass  # не шумим в выводе тестов


class DownloadTests(unittest.TestCase):
    """download() против локального сервера (пункты 1–2)."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _SpeedHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        # download() в продакшене отвергает 127.0.0.1 (защита от запросов
        # к локальным/приватным хостам), поэтому только здесь подменяем
        # валидатор заглушкой: проверяем транспорт, а не эту защиту.
        patcher = unittest.mock.patch("speedtest._validate_url",
                                      lambda url: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_download_exact_bytes(self):
        nbytes, seconds = speedtest.download(self.base + "/bytes/50000", 5)
        self.assertEqual(nbytes, 50000)
        self.assertGreater(seconds, 0.0)
        self.assertLess(seconds, 5.0)

    def test_download_without_content_length(self):
        nbytes, _ = speedtest.download(self.base + "/stream/12345", 5)
        self.assertEqual(nbytes, 12345)

    def test_download_404_raises(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            speedtest.download(self.base + "/missing", 5)
        ctx.exception.close()  # HTTPError файлоподобен — закрываем за собой


class ValidateUrlTests(unittest.TestCase):
    """Санитарная проверка самого валидатора (офлайн, без DNS)."""

    def test_rejects_wrong_scheme(self):
        with self.assertRaises(ValueError):
            speedtest._validate_url("ftp://example.com/file")

    def test_rejects_loopback_ip(self):
        with self.assertRaises(ValueError):
            speedtest._validate_url("http://127.0.0.1/x")


class AverageAndSpeedTests(unittest.TestCase):
    """Среднее время и скорость на фиксированных числах (пункт 3)."""

    def test_average(self):
        self.assertAlmostEqual(speedtest.average([1.5, 2.0, 2.5]), 2.0)

    def test_average_empty_is_zero(self):
        self.assertEqual(speedtest.average([]), 0.0)

    def test_speed_mbs(self):
        self.assertAlmostEqual(speedtest.speed(1_000_000, 1.0, "mbs"), 1.0)

    def test_speed_mbps(self):
        self.assertAlmostEqual(speedtest.speed(2_500_000, 2.0, "mbps"), 10.0)

    def test_speed_zero_bytes(self):
        self.assertEqual(speedtest.speed(0, 5.0), 0.0)

    def test_speed_zero_seconds_guard(self):
        self.assertEqual(speedtest.speed(12345, 0.0), 0.0)


class ConvertSpeedTests(unittest.TestCase):
    """Конвертация единиц, включая нули (пункт 4)."""

    def test_mbs_to_mbps(self):
        self.assertAlmostEqual(speedtest.convert_speed(1.0, "mbs", "mbps"), 8.0)

    def test_mbps_to_mbs(self):
        self.assertAlmostEqual(speedtest.convert_speed(8.0, "mbps", "mbs"), 1.0)

    def test_mbs_to_kbs(self):
        self.assertAlmostEqual(speedtest.convert_speed(1.0, "mbs", "kbs"), 1000.0)

    def test_kbs_to_mbs(self):
        self.assertAlmostEqual(speedtest.convert_speed(500.0, "kbs", "mbs"), 0.5)

    def test_mbps_to_kbs(self):
        self.assertAlmostEqual(speedtest.convert_speed(8.0, "mbps", "kbs"), 1000.0)

    def test_Bps_to_kbs(self):
        self.assertAlmostEqual(speedtest.convert_speed(500.0, "Bps", "kbs"), 0.5)

    def test_same_unit_identity(self):
        self.assertAlmostEqual(speedtest.convert_speed(3.14, "mbs", "mbs"), 3.14)

    def test_zero_value(self):
        self.assertEqual(speedtest.convert_speed(0.0, "mbs", "mbps"), 0.0)

    def test_unknown_unit_raises(self):
        with self.assertRaises(ValueError):
            speedtest.convert_speed(1.0, "mbs", "mph")


class WithSizeTests(unittest.TestCase):
    """Подмена bytes= по --size (пункт 5)."""

    def test_replaces_existing_bytes(self):
        self.assertEqual(
            speedtest.with_size("https://h.test/__down?bytes=1&x=2", 5),
            "https://h.test/__down?bytes=5000000&x=2",
        )

    def test_adds_to_url_without_query(self):
        self.assertEqual(
            speedtest.with_size("https://h.test/file.bin", 2),
            "https://h.test/file.bin?bytes=2000000",
        )

    def test_foreign_url_untouched_except_param(self):
        # чужой сервер параметр проигнорирует — функция лишь дописывает его
        self.assertEqual(
            speedtest.with_size("https://example.com/big.bin", 5),
            "https://example.com/big.bin?bytes=5000000",
        )

    def test_fractional_size(self):
        self.assertEqual(
            speedtest.with_size("https://h.test/", 1.5),
            "https://h.test/?bytes=1500000",
        )

    def test_preserves_other_params_and_fragment(self):
        self.assertEqual(
            speedtest.with_size("https://h.test/p?a=1#frag", 1),
            "https://h.test/p?a=1&bytes=1000000#frag",
        )

    def test_http_scheme(self):
        self.assertEqual(
            speedtest.with_size("http://h.test/d", 3),
            "http://h.test/d?bytes=3000000",
        )


class ProgressLineTests(unittest.TestCase):
    """Строка прогрессбара (пункт 6)."""

    def test_with_total_exact_format(self):
        line = speedtest.progress_line(45_000_000, 100_000_000, 3, 10, 12.3, 20)
        self.assertEqual(line, "3/10 [" + "#" * 9 + "." * 11 + "]  45%  12.3 МБ/с")

    def test_zero_percent(self):
        self.assertEqual(
            speedtest.progress_line(0, 100, 1, 10, 0.0, 5),
            "1/10 [.....]   0%  0.0 МБ/с",
        )

    def test_clamps_to_100_percent(self):
        line = speedtest.progress_line(150, 100, 1, 10, 9.9, 5)
        self.assertEqual(line, "1/10 [#####] 100%  9.9 МБ/с")

    def test_without_total_no_bar_no_percent(self):
        line = speedtest.progress_line(5_000_000, None, 2, 10, 8.0, 20)
        self.assertEqual(line, "2/10  5.00 МБ  8.0 МБ/с")
        self.assertNotIn("[", line)
        self.assertNotIn("%", line)

    def test_unit_labels(self):
        line = speedtest.progress_line(45, 100, 3, 10, 12.3, 3, unit="mbps")
        self.assertTrue(line.endswith("12.3 Мбит/с"))
        line = speedtest.progress_line(45, 100, 3, 10, 12.3, 3, unit="kbs")
        self.assertTrue(line.endswith("12.3 КБ/с"))


class SummaryDataTests(unittest.TestCase):
    """Строитель итогового словаря (пункт 7), фиксированные числа:

    3 запроса, 15 000 000 байт, времена [1.5, 2.0, 2.5] с.
    """

    TIMES = [1.5, 2.0, 2.5]

    def test_fields_default_unit_mbs(self):
        data = speedtest.summary_data(3, 15_000_000, self.TIMES, "mbs")
        self.assertEqual(data["requests"], 3)
        self.assertEqual(data["total_bytes"], 15_000_000)
        self.assertAlmostEqual(data["total_mb"], 15.0)
        self.assertAlmostEqual(data["total_seconds"], 6.0)
        self.assertAlmostEqual(data["avg_seconds"], 2.0)
        self.assertAlmostEqual(data["speed"], 2.5)      # 15 МБ / 6 с
        self.assertEqual(data["unit"], "mbs")
        self.assertAlmostEqual(data["speed_extra"], 20.0)  # 2.5 * 8
        self.assertEqual(data["unit_extra"], "mbps")

    def test_unit_mbps_swaps_extra(self):
        data = speedtest.summary_data(3, 15_000_000, self.TIMES, "mbps")
        self.assertAlmostEqual(data["speed"], 20.0)
        self.assertEqual(data["unit"], "mbps")
        self.assertAlmostEqual(data["speed_extra"], 2.5)
        self.assertEqual(data["unit_extra"], "mbs")

    def test_empty_times_no_division_error(self):
        data = speedtest.summary_data(0, 0, [], "mbs")
        self.assertEqual(data["total_seconds"], 0.0)
        self.assertEqual(data["avg_seconds"], 0.0)
        self.assertEqual(data["speed"], 0.0)
        self.assertEqual(data["speed_extra"], 0.0)


class MainExitCodeTests(unittest.TestCase):
    """Коды выхода main() на плохом вводе: контракт README.

    Неверные аргументы — SystemExit(2) от argparse (включая nan/inf
    в числовых флагах); битый URL и запрещённые адреса — чистый
    код 1 с сообщением в stderr, без traceback'а.
    """

    def _expect_argparse_error(self, argv):
        """main(argv) должен упасть с argparse-ошибкой (код выхода 2)."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                speedtest.main(argv)
        self.assertEqual(ctx.exception.code, 2)

    def _run_main(self, argv):
        """main(argv) с заглушённым stderr: сообщение не шумит в выводе."""
        with contextlib.redirect_stderr(io.StringIO()):
            return speedtest.main(argv)

    def test_size_inf_rejected(self):
        self._expect_argparse_error(["--size", "inf"])

    def test_size_nan_rejected(self):
        self._expect_argparse_error(["--size", "nan"])

    def test_timeout_nan_rejected(self):
        self._expect_argparse_error(["--timeout", "nan"])

    def test_malformed_url_clean_exit(self):
        # раньше: ValueError из urlsplit в with_size падал traceback'ом
        self.assertEqual(self._run_main(["http://[::1"]), 1)

    def test_loopback_url_clean_exit(self):
        # защита _validate_url: локальный адрес — чистый код 1
        self.assertEqual(self._run_main(["http://127.0.0.1/x"]), 1)


if __name__ == "__main__":
    unittest.main()
