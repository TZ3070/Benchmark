"""Start the Benchmark HTTP API."""
import logging
from http.server import ThreadingHTTPServer

from .api import Handler
from .core import HOST, PORT


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("benchmark.server")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("event=server_started host=%s port=%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("event=server_stopping reason=keyboard_interrupt")
    finally:
        server.server_close()
        logger.info("event=server_stopped")
