"""Assembly of all connector parts and the CLI entry point."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__, audit as audit_mod, config as config_mod, pairing, protocol
from . import security, tools
from .api import ApiServer, AppContext
from .config import ConfigStore
from .jobs import JobManager
from .proxy import BlenderLink
from .relay import make_default_relay
from .tunnel import CloudflaredTunnel, DisabledTunnel

BANNER = r"""
  ____  _                 _            ____       _     _
 | __ )| | ___ _ __   ___| |__   ___  | __ ) _ __(_) __| | __ _  ___
 |  _ \| |/ _ \ '_ \ / _ \ '_ \ / _ \ |  _ \| '__| |/ _` |/ _` |/ _ \
 | |_) | |  __/ | | |  __/ |_) |  __/ | |_) | |  | | (_| | (_| |  __/
 |____/|_|\___|_| |_|\___|_.__/ \___| |____/|_|  |_|\__,_|\__, |\___|
                                                          |___/
"""


class ConnectorApp:
    """Owns every runtime object and their start/stop order."""

    def __init__(self, *, config_dir: Optional[Path] = None, port: Optional[int] = None,
                 auto_open: bool = True, headless: bool = False,
                 addon_host: str = "127.0.0.1", addon_port: Optional[int] = None,
                 verbose: bool = False):
        self.store = ConfigStore(config_dir)
        self.config = self.store.load()
        self.store.ensure_dirs()
        self.headless = headless
        self.auto_open = auto_open
        self.verbose = verbose
        if port:
            self.config.http_port = int(port)
        if addon_port:
            self.config.addon_port = int(addon_port)
        self.config.addon_host = addon_host

        self.debug = audit_mod.DEBUG
        self.log = audit_mod.setup_file_logger(self.store.log_dir, "connector")
        for warning in self.store.warnings:
            self.debug.add(warning, "warn")
            self.log.warning(warning)

        self.audit = audit_mod.AuditLog(self.store.log_dir) if self.config.audit_log else None
        self.ui_token = security.random_token(24)
        self.addon_token = pairing.load_or_create(self.store.dir)
        self.limiter = security.RateLimiter(self.config.rate_limit_per_min)

        # agent keys survive restarts (stored hashed inside the secrets blob)
        try:
            stored_keys = json.loads(self.store.get_secret("agent_keys") or "[]")
        except ValueError:
            stored_keys = []
        self.keys = security.KeyStore(stored_keys)

        self.policy = security.ApprovalPolicy(
            threshold=self.config.approval_threshold,
            trust_window_s=self.config.trust_window_s,
            allow_trust_window=self.config.allow_trust_window,
        )

        self.link = BlenderLink(
            self.config.addon_host, self.config.addon_port, self.addon_token,
            on_state_change=self._on_blender_state, debug=self.debug,
        )

        self.jobs = JobManager(
            config=self.config,
            policy=self.policy,
            blender_call=self._blender_call,
            blender_status=self.link.status,
            audit=self.audit,
            job_dir=self.store.job_dir,
            on_event=self._on_job_event,
        )

        self.tunnel = (CloudflaredTunnel(local_port=self.config.http_port, config=self.config,
                                         app_dir=config_mod.app_dir(), debug=self.debug)
                       if self.config.tunnel_provider == "cloudflared"
                       else DisabledTunnel())

        self.relay = make_default_relay(jobs=self.jobs, config_store=self.store,
                                        audit=self.audit, debug=self.debug,
                                        artifact_root=self.store.job_dir)

        if not self.keys.list():
            self._first_key, rec = self.keys.create("first-run-agent")
            self.store.set_secret("agent_keys", json.dumps(self.keys.export()))
            self.store.save()
            self.debug.add("Erster Agent-Schlüssel erzeugt (wird im Fenster angezeigt)", "info")
        else:
            self._first_key = ""

        self.api: Optional[ApiServer] = None
        self.context: Optional[AppContext] = None
        self._stop = threading.Event()

    # -- callbacks ---------------------------------------------------------
    def _blender_call(self, tool: str, params: Dict[str, Any], *,
                      timeout: float = protocol.DEFAULT_JOB_TIMEOUT,
                      dry_run: bool = False, meta: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
        return self.link.call(tool, params, timeout=timeout, dry_run=dry_run, meta=meta)

    def _on_blender_state(self, state: Dict[str, Any]) -> None:
        self.audit and self.audit.write({"event": "blender_state", **state})

    def _on_job_event(self, event: Dict[str, Any]) -> None:
        if self.verbose:
            self.debug.add(f"Job-Event: {event.get('type')} {event.get('job')}")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        self.link.start()
        self.jobs.start()
        self.context = AppContext(
            config=self.config, store=self.store, jobs=self.jobs, link=self.link,
            keys=self.keys, audit=self.audit, tunnel=self.tunnel, relay=self.relay,
            version=__version__, ui_token=self.ui_token, debug=self.debug,
            limiter=self.limiter, config_changed=self._on_config_changed,
            addon_token=self.addon_token,
        )
        self.api = ApiServer(
            self.context, host=self.config.bind_host, port=self.config.http_port,
        )
        host, port = self.api.start()
        self.config.http_port = port
        self.tunnel.local_port = port
        if self.config.tunnel_provider == "cloudflared":
            self.tunnel.local_port = port

        if self.config.relay_enabled:
            self.relay.start()

        url = f"{self.api.scheme}://{host}:{port}/"
        summary = {
            "ok": True,
            "url": url,
            "port": port,
            "ui_token": self.ui_token,
            "addon_token": self.addon_token,
            "config_dir": str(self.store.dir),
            "agent_key": self._first_key,
            "blender_port": self.config.addon_port,
            "tunnel_provider": self.config.tunnel_provider,
        }
        self.audit and self.audit.write({
            "event": "connector_started", "version": __version__,
            "port": port, "config_dir": str(self.store.dir),
            "python": sys.version.split()[0], "os": platform.platform(),
        })
        if self.auto_open and not self.headless:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        return summary

    def _on_config_changed(self, cfg) -> None:
        self.limiter.per_minute = cfg.rate_limit_per_min
        self.policy.threshold = cfg.approval_threshold
        self.policy.allow_trust_window = cfg.allow_trust_window
        self.policy.trust_window_s = cfg.trust_window_s
        if cfg.relay_enabled and not self.relay.status()["enabled"]:
            self.relay.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.jobs.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.link.stop()
        except Exception:  # noqa: BLE001
            pass
        for component in (self.relay, self.tunnel, self.api):
            try:
                if component:
                    component.stop()
            except Exception:  # noqa: BLE001
                pass
        self.audit and self.audit.write({"event": "connector_stopped"})

    def run_forever(self) -> int:
        info = self.start()
        self.print_startup(info)
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nBeende BlenderBridge ...")
        finally:
            self.stop()
        return 0

    # -- console output ----------------------------------------------------
    def print_startup(self, info: Dict[str, Any]) -> None:
        print(BANNER)
        print(f"  BlenderBridge {__version__}   Protokoll {protocol.PROTOCOL_VERSION}")
        print("  " + "-" * 66)
        print(f"  Oberfläche (Browser):   {info['url']}")
        print(f"  Konfiguration/Logs:     {info['config_dir']}")
        print(f"  Blender-Add-on-Port:    {self.config.addon_port} (nur localhost)")
        print()
        print("  So verbindest du Blender:")
        print("   1. Add-on 'blenderbridge_addon.zip' in Blender installieren")
        print("   2. In Blender: N-Seitenleiste -> BlenderBridge -> 'Server starten'")
        print("   3. Der Token wird automatisch aus der gemeinsamen Datei gelesen;")
        print(f"      falls nicht: '{info['addon_token']}' im Add-on eintragen.")
        if info.get("agent_key"):
            print()
            print("  Agent-Schlüssel (nur jetzt sichtbar, auch im Browser unter Einstellungen):")
            print(f"   {info['agent_key']}")
        print()
        print("  Not-Aus: im Browser oben rechts auf 'NOT-AUS' klicken.")
        print("  Beenden: Strg+C in diesem Fenster.")
        print("  " + "-" * 66)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blenderbridge",
        description="BlenderBridge - verbindet KI-Agenten mit einer geöffneten Blender-Szene.",
    )
    p.add_argument("--port", type=int, help="Port für die lokale Weboberfläche/API (Standard 8765)")
    p.add_argument("--addon-port", type=int, help="Port des Blender-Add-ons (Standard 9876)")
    p.add_argument("--addon-host", default="127.0.0.1")
    p.add_argument("--config-dir", type=Path, help="Eigener Ordner für Konfiguration/Logs")
    p.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    p.add_argument("--headless", action="store_true", help="Nur API, keine automatische Oberfläche")
    p.add_argument("--verbose", action="store_true", help="Mehr Details im Log")
    p.add_argument("--version", action="store_true", help="Version ausgeben und beenden")
    p.add_argument("--selftest", action="store_true",
                   help="Interne Selbsttests ohne Blender ausführen")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"BlenderBridge {__version__}")
        return 0
    if args.selftest:
        from .selftest import run_selftest  # local import: only needed here
        return run_selftest()

    if not args.headless and os.name == "nt":
        _setup_windows_console()

    app = ConnectorApp(
        config_dir=args.config_dir, port=args.port, auto_open=not args.no_browser,
        headless=args.headless, addon_host=args.addon_host,
        addon_port=args.addon_port, verbose=args.verbose,
    )

    def _sigterm(signum, frame):  # noqa: ANN001, ARG001
        app._stop.set()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _sigterm)
        except (ValueError, OSError):
            pass
    return app.run_forever()


def _setup_windows_console() -> None:
    """Make sure German umlauts render and the title bar says what this is."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("BlenderBridge - nicht schließen")  # type: ignore[attr-defined]
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
