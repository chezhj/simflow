"""
Unit tests for the xFlow/net_probe command.

The probe exists to answer a question the plugin's own "state response in N ms"
line cannot: of a slow round trip, how much is DNS, how much is connection
setup, and how much is the server. It runs on the HTTP worker thread and only
writes to the log, so what is worth testing is that it reports every stage, and
that it fails safely — a probe that crashes the worker thread would take the
plugin's entire HTTP path down with it.
"""

import importlib.util
import pathlib
import socket
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_xp_stub():
    xp = types.ModuleType("xp")
    xp.Type_Data = 32
    xp.findDataRef = MagicMock()
    xp.getDataRefTypes = MagicMock(return_value=0)
    xp.getDataf = MagicMock(return_value=0.0)
    xp.getDatad = MagicMock(return_value=0.0)
    xp.getDatas = MagicMock(return_value="")
    xp.getDatavf = MagicMock()
    xp.log = MagicMock()
    xp.getSystemPath = MagicMock(return_value="/tmp/xplane")
    xp.registerFlightLoopCallback = MagicMock()
    xp.unregisterFlightLoopCallback = MagicMock()
    xp.createCommand = MagicMock(return_value=object())
    xp.registerCommandHandler = MagicMock()
    xp.unregisterCommandHandler = MagicMock()
    return xp


class _ProbeTestBase(unittest.TestCase):

    def setUp(self):
        self.xp_stub = _make_xp_stub()
        sys.modules["xp"] = self.xp_stub
        sys.modules["XPPython3"] = types.ModuleType("XPPython3")
        sys.modules["XPPython3.xp"] = self.xp_stub
        for name in ("xplane_plugin.xFlow.PI_xFlow", "PI_xFlow"):
            sys.modules.pop(name, None)

        spec = importlib.util.spec_from_file_location(
            "PI_xFlow",
            pathlib.Path(__file__).parent.parent / "xFlow" / "PI_xFlow.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

        with patch("configparser.ConfigParser.read"):
            self.plugin = self.module.PythonInterface()
        self.plugin._api_key = "test-key"
        self.plugin._backend_url = "https://example.test"
        self.plugin._log_level = 0  # DEBUG, so every line is emitted

    def logged(self):
        return "\n".join(c.args[0] for c in self.xp_stub.log.call_args_list)


class TestNetProbeReporting(_ProbeTestBase):

    def _patch_network(self, addrinfo=None):
        """
        Make every stage succeed instantly. getaddrinfo is patched because the
        probe resolves a hostname that must never be looked up for real.
        """
        addrinfo = addrinfo or [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        ]
        return (
            patch.object(socket, "getaddrinfo", return_value=addrinfo),
            patch.object(self.module, "socket", wraps=socket),
        )

    def test_reports_every_stage(self):
        """Each stage appears in the log with a timing, over https."""
        fake_sock = MagicMock()
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        ]), patch.object(socket, "socket", return_value=fake_sock), \
                patch.object(self.module.ssl, "create_default_context"), \
                patch.object(self.module.PythonInterface, "_http_get",
                             return_value=(200, {})), \
                patch.object(self.module, "_USE_REQUESTS", False):
            self.plugin._run_net_probe()

        out = self.logged()
        for stage in ("DNS resolve", "TCP connect", "TCP + TLS handshake",
                      "GET /  (no auth, no work)", "GET /api/plugin/session/"):
            self.assertIn(stage, out, f"{stage!r} missing from probe output")
        self.assertIn("net probe done", out)

    def test_plain_http_skips_tls_stage(self):
        """No TLS handshake is timed when the backend is not https."""
        self.plugin._backend_url = "http://example.test:8000"
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 8000))
        ]), patch.object(socket, "socket", return_value=MagicMock()), \
                patch.object(self.module.PythonInterface, "_http_get",
                             return_value=(200, {})), \
                patch.object(self.module, "_USE_REQUESTS", False):
            self.plugin._run_net_probe()

        self.assertNotIn("TLS handshake", self.logged())

    def test_placeholder_key_skips_authenticated_call(self):
        """An unconfigured key must not produce a guaranteed-401 measurement."""
        self.plugin._api_key = self.module.PLACEHOLDER
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        ]), patch.object(socket, "socket", return_value=MagicMock()), \
                patch.object(self.module.ssl, "create_default_context"), \
                patch.object(self.module.PythonInterface, "_http_get",
                             return_value=(200, {})), \
                patch.object(self.module, "_USE_REQUESTS", False):
            self.plugin._run_net_probe()

        out = self.logged()
        self.assertIn("skipping the authenticated call", out)
        self.assertNotIn("GET /api/plugin/session/", out)


class TestNetProbeFailsSafely(_ProbeTestBase):

    def test_unparseable_url_returns_without_probing(self):
        self.plugin._backend_url = "not a url"
        self.plugin._run_net_probe()
        out = self.logged()
        self.assertIn("cannot parse backend_url", out)
        self.assertNotIn("DNS resolve", out)

    def test_unresolvable_host_returns_after_dns_stage(self):
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("nope")):
            self.plugin._run_net_probe()
        out = self.logged()
        self.assertIn("DNS resolve", out)
        self.assertIn("cannot resolve", out)
        self.assertNotIn("TCP connect", out)

    def test_refused_connection_abandons_the_probe(self):
        """
        A socket that will not open makes every later stage a guaranteed
        timeout. Five samples x four stages x a 5 s timeout is 100 s of a held
        worker thread, so the probe stops instead.
        """
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        ]), patch.object(socket, "socket",
                         side_effect=ConnectionRefusedError("refused")):
            self.plugin._run_net_probe()

        out = self.logged()
        self.assertIn("TCP connect", out)
        self.assertIn("abandoned: cannot open a socket", out)
        self.assertNotIn("GET /", out)

    def test_long_error_text_is_truncated(self):
        """requests wraps a refused connection in a multi-line urllib3 blob."""
        long_message = "x" * 500
        with patch.object(socket, "getaddrinfo",
                          side_effect=RuntimeError(long_message)):
            self.plugin._run_net_probe()

        for call_args in self.xp_stub.log.call_args_list:
            self.assertLess(len(call_args.args[0]), 250,
                            "probe emitted an unbounded error line")

    def test_http_failure_does_not_stop_later_stages(self):
        """One dead endpoint must not hide the rest of the breakdown."""
        with patch.object(socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        ]), patch.object(socket, "socket", return_value=MagicMock()), \
                patch.object(self.module.ssl, "create_default_context"), \
                patch.object(self.module.PythonInterface, "_http_get",
                             side_effect=OSError("boom")), \
                patch.object(self.module, "_USE_REQUESTS", False):
            self.plugin._run_net_probe()

        out = self.logged()
        self.assertIn("GET /  (no auth, no work)", out)
        self.assertIn("GET /api/plugin/session/", out)
        self.assertIn("net probe done", out)


class TestNetProbeDispatch(_ProbeTestBase):

    def test_command_runs_off_the_flight_loop(self):
        """
        The probe makes blocking socket calls, so the command handler must hand
        it to the worker queue rather than run it inline on the flight loop.
        """
        with patch.object(self.module.PythonInterface, "_submit",
                          return_value=True) as submit:
            self.plugin._on_net_probe(0)

        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0], self.plugin._run_net_probe)

    def test_only_fires_on_command_begin(self):
        with patch.object(self.module.PythonInterface, "_submit") as submit:
            self.plugin._on_net_probe(1)
            self.plugin._on_net_probe(2)
        submit.assert_not_called()

    def test_full_queue_is_reported(self):
        with patch.object(self.module.PythonInterface, "_submit",
                          return_value=False):
            self.plugin._on_net_probe(0)
        self.assertIn("HTTP backlog full", self.logged())


if __name__ == "__main__":
    unittest.main()
