"""Local setup and two-listener server entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import signal
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .admin import HASHER, create_admin
from .api import create_api
from .service import ProviderService
from .storage import Store
from .updater import Updater


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):  # Both listeners share one process and signal handler.
        yield


async def serve(store: Store, args: argparse.Namespace) -> None:
    service = ProviderService(store, public_base_url=args.public_base_url, concurrency=args.concurrency)
    updater = Updater(store)
    api = ManagedServer(uvicorn.Config(create_api(service), host=args.api_host, port=args.api_port,
                                       log_level="info", proxy_headers=False))
    admin = ManagedServer(uvicorn.Config(create_admin(service, updater, api_port=args.api_port,
                                                     cookie_secure=args.admin_cookie_secure),
                                         host=args.admin_host, port=args.admin_port,
                                         log_level="info", proxy_headers=False))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, lambda: (setattr(api, "should_exit", True),
                                                     setattr(admin, "should_exit", True)))
        except NotImplementedError:  # pragma: no cover - Windows fallback
            pass
    updater.start_scheduler()
    tasks = [asyncio.create_task(api.serve()), asyncio.create_task(admin.serve())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        api.should_exit = True
        admin.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.stop()
        await updater.stop_scheduler()


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-hosted YouTube SongProvider v0")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("YSP_DATA_DIR", "./data")))
    sub = parser.add_subparsers(dest="command", required=True)
    admin_parser = sub.add_parser("admin", help="local administrative setup")
    admin_parser.add_argument("action", choices=["set-password"])
    serve_parser = sub.add_parser("serve", help="start the API and local administration listeners")
    serve_parser.add_argument("--api-host", default=os.environ.get("YSP_API_HOST", "127.0.0.1"))
    serve_parser.add_argument("--api-port", type=int, default=int(os.environ.get("YSP_API_PORT", "3030")))
    serve_parser.add_argument("--admin-host", default=os.environ.get("YSP_ADMIN_HOST", "127.0.0.1"))
    serve_parser.add_argument("--admin-port", type=int, default=int(os.environ.get("YSP_ADMIN_PORT", "3031")))
    serve_parser.add_argument("--public-base-url", default=os.environ.get("YSP_PUBLIC_BASE_URL"))
    serve_parser.add_argument("--admin-cookie-secure", action="store_true",
                              default=os.environ.get("YSP_ADMIN_COOKIE_SECURE") == "1")
    serve_parser.add_argument("--concurrency", type=int, default=int(os.environ.get("YSP_CONCURRENCY", "2")))
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        if args.command == "admin":
            password = getpass.getpass("New admin password (at least 12 characters): ")
            confirm = getpass.getpass("Confirm password: ")
            if len(password) < 12 or password != confirm:
                parser.error("passwords must match and contain at least 12 characters")
            store.set_setting("admin_password_hash", HASHER.hash(password))
            print("Admin password set.")
        else:
            if args.concurrency < 1 or args.concurrency > 16:
                parser.error("concurrency must be between 1 and 16")
            if args.api_port == args.admin_port and args.api_host == args.admin_host:
                parser.error("API and administration must use different listeners")
            asyncio.run(serve(store, args))
    finally:
        store.close()


if __name__ == "__main__":
    main()
