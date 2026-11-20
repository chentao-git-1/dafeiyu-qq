"""Regression checks for the host watchdog's timestamped Docker log reader."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import watchdog_errors as watchdog


def record(when, *lines):
    return "\n".join(f"2026-11-20T10:43:{when}Z {line}" for line in lines) + "\n"


DASH = "[18:43:55.277] [Core] [ERRO] [v4] [api.app:184]: Unhandled exception in dashboard API"
MIRROR = "[18:43:55.318] [Core] [ERRO] [hypercorn.logging:115]: Error in ASGI Framework"
SERVER = "[2026-11-20 18:43:55 +0800] [1] [ERROR] Error in ASGI Framework"
MODEL = "[18:43:56.939] [Core] [ERRO] [utils.network_utils:86]: [OpenAI] Network connection failed"
CHAIN = (watchdog.TRACE, "ValueError: input", "During handling of the above exception, another exception occurred:",
         watchdog.TRACE, "RuntimeError: no response")


class WatchdogErrors(unittest.TestCase):
    def test_dashboard_exception_mirrors_and_chained_provider_error(self):
        stdout = record("55.200000001", DASH, *CHAIN) + record("55.318", MIRROR, *CHAIN)
        stdout += record("56.939", MODEL)
        stdout += record("56.990", "[18:43:56.939] [Core] [WARN] [tool_runner:20]: request error",
                         *CHAIN, watchdog.TRACE, "openai.APIConnectionError: connection error")
        stderr = record("55.318000001", SERVER, *CHAIN)
        self.assertEqual(sum("Traceback" in s or "[ERROR]" in s for s in (stdout+stderr).splitlines()), 10)
        self.assertEqual(watchdog.error_counts(stdout, stderr), {"管理面板": 1, "模型连接": 1})

    def test_independent_errors_survive_even_when_simultaneous_and_identical(self):
        stdout = record("55.001", DASH, *CHAIN) * 2 + record("55.002", MIRROR, *CHAIN) * 2
        self.assertEqual(watchdog.error_counts(stdout, record("55.002", SERVER, *CHAIN)*2), {"管理面板": 2})
        self.assertEqual(watchdog.error_counts(record("55", MODEL)*3, ""), {"模型连接": 3})
        self.assertEqual(watchdog.error_counts(record("55", DASH, *CHAIN),
                         record("55", SERVER, watchdog.TRACE, "TypeError: different")), {"管理面板": 1, "服务框架": 1})

    def test_unhandled_stderr_exceptions_and_text_inside_info(self):
        raw = record("56", *CHAIN, watchdog.TRACE, "RuntimeError: independent")
        info = record("55", "[18:43:55.000] [Core] [INFO] [logger:19]: user typed [ERROR] Traceback")
        self.assertEqual(watchdog.error_counts(info, raw), {"其他服务": 2})

    def test_threshold_cooldown_and_previous_state(self):
        notify, log = Mock(), Mock()
        state = {"errors": {"last_check": "0", "last_alert": None}}
        def check():
            watchdog.check_recent_errors(state, notify, log, interval=600, threshold=3)
        with patch.object(watchdog.time, "time", side_effect=[10000,10601,11202]), patch.object(watchdog.subprocess, "run") as docker:
            docker.return_value = subprocess.CompletedProcess([],0,record("55",DASH,*CHAIN)+record("55",MIRROR,*CHAIN),record("55",SERVER,*CHAIN))
            check()
            notify.assert_not_called()
            docker.return_value = subprocess.CompletedProcess([],0,record("55",MODEL)*3,"")
            check()
            check()
        notify.assert_called_once()
        self.assertIn("模型连接 3 次", notify.call_args.args[0])


if __name__ == "__main__":
    sys.argv[1:] = [arg for arg in sys.argv[1:] if not Path(arg).is_dir()]
    unittest.main()
