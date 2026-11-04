"""Run the optional dashboard behind an SSH tunnel or trusted TLS reverse proxy."""

import argparse
import os

from aer import AER, load_deployment_config


def main() -> None:
    import uvicorn

    from aer.dashboard.app import create_app

    parser = argparse.ArgumentParser(description="AER read-only dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    password = os.environ.get("AER_DASHBOARD_PASSWORD", "")
    secret = os.environ.get("AER_DASHBOARD_SESSION_SECRET", "")
    if len(password) < 16 or len(secret) < 32:
        parser.error("Set AER_DASHBOARD_PASSWORD (>=16) and AER_DASHBOARD_SESSION_SECRET (>=32)")
    config = load_deployment_config()
    with AER(
        config.db_path.parent, db_filename=config.db_path.name, knowledge_dir=config.knowledge_dir
    ) as aer:
        uvicorn.run(
            create_app(aer, password, secret),
            host=args.host,
            port=args.port,
            access_log=False,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
