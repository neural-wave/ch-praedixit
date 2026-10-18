"""Start the REST API of the project (POST /nl-to-sparql, see pppp/src/api.py).

The models of the pipeline (Gate A and the generator, see config/config.yaml) are loaded once,
then a warm-up request is translated before the server accepts requests. The LLM server named in
the generator's config.json (Ollama by default) must be running.

Usage: python expose.py [--host 127.0.0.1] [--port 8000]
"""

import argparse
import logging

import uvicorn

from pppp.api import create_app
from pppp.pipeline import Pipeline

# Translated once at startup, so that the first real request does not wait for the LLM server
# to load its model.
WARM_UP_REQUEST = "How many patients have a Billed Diagnosis for Type 2 diabetes mellitus?"


def main():
    argument_parser = argparse.ArgumentParser(description="Serve POST /nl-to-sparql.")
    argument_parser.add_argument("--host", default="127.0.0.1",
                                 help="address to listen on (0.0.0.0 for every network interface)")
    argument_parser.add_argument("--port", type=int, default=8000)
    arguments = argument_parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    pipeline = Pipeline.load()
    pipeline.run(WARM_UP_REQUEST)
    uvicorn.run(create_app(pipeline), host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
