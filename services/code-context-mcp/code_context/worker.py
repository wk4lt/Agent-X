"""Detached durable build runner; survives a short-lived stdio MCP connection."""
import argparse
import json
from .service import CodeService

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--roots', required=True)
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    CodeService(args.data, json.loads(args.roots)).worker(args.job)
