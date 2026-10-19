"""Read tools by default; operator-controlled discovery/build maintenance mode."""
import argparse
import asyncio
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .service import CodeService


def create_server(data, roots, writable=False, allowed_projects=(), host='127.0.0.1', port=8766):
    service = CodeService(data, roots, allowed_projects)
    mcp = FastMCP('Agent-X Code Context', host=host, port=port)

    @mcp.tool()
    async def code_search(project_id: str, baseline_id: str, repo_id: str, query: str,
                          limit: int = 8, component_id: str | None = None) -> dict:
        """Search CodeGraph in a published immutable baseline. Scores are rankings, not confidence."""
        return await asyncio.to_thread(service.search, project_id, baseline_id, repo_id, query, limit, component_id)

    @mcp.tool()
    async def code_read(project_id: str, baseline_id: str, repo_id: str, path: str,
                        start_line: int = 1, end_line: int = 120) -> dict:
        """Read line-numbered source from the indexed snapshot, never the mutable working tree."""
        return await asyncio.to_thread(service.read, project_id, baseline_id, repo_id, path, start_line, end_line)

    @mcp.tool()
    async def code_relations(project_id: str, baseline_id: str, repo_id: str, node_id: str,
                             direction: str = 'callers', depth: int = 1) -> dict:
        """Bounded static callers/callees within one repository; not proof of a runtime path."""
        return await asyncio.to_thread(service.relations, project_id, baseline_id, repo_id, node_id, direction, depth)

    @mcp.tool()
    async def code_status(project_id: str, baseline_id: str, job_id: str) -> dict:
        """Inspect durable build progress, coverage, revisions and explicit failure details."""
        return await asyncio.to_thread(service.status, project_id, baseline_id, job_id)

    if writable:
        @mcp.tool()
        async def code_discover(root_id: str, relative_path: str = '.', max_files: int = 1000) -> dict:
            """Bounded discovery under operator-registered roots; architecture remains unconfirmed."""
            return await asyncio.to_thread(service.discover, root_id, relative_path, max_files)

        @mcp.tool()
        async def code_build(project_id: str, baseline_id: str, config: dict, retry: bool = False) -> dict:
            """Submit an idempotent detached build from project_get's confirmed config; poll code_status."""
            return await asyncio.to_thread(service.build, project_id, baseline_id, config, retry)
    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--roots', required=True, help='Operator JSON file mapping root IDs to absolute directories')
    parser.add_argument('--project', action='append', default=[], help='Allowed project; repeat to allow several')
    parser.add_argument('--writable', action='store_true')
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--host', choices=['127.0.0.1', 'localhost', '::1'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    roots = json.loads(Path(args.roots).read_text())
    create_server(args.data, roots, args.writable, args.project, args.host, args.port).run(transport=args.transport)


if __name__ == '__main__':
    main()
