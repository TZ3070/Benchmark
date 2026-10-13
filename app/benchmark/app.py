"""Start the Benchmark HTTP API."""
from http.server import ThreadingHTTPServer

from .api import Handler
from .core import HOST, PORT


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
