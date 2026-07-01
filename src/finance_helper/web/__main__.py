"""python -m finance_helper.web — start the local review UI."""

import os

from .app import create_app


def main():
    app = create_app()
    host = os.environ.get("FINANCE_HELPER_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("FINANCE_HELPER_WEB_PORT", "5000"))
    debug = os.environ.get("FINANCE_HELPER_WEB_DEBUG", "").lower() in ("1", "true", "yes")
    print(f"Finance Helper review UI: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
