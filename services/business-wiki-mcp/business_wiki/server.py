"""MCP entry point. Optional strict project mode; legacy mode stays compatible."""
import argparse
import asyncio
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .store import Store
from .projects import ProjectCatalog
from .compiler import compile_source


def create_server(db_path, writable=False, host='127.0.0.1', port=8765,
                  projects_root=None, allowed_projects=()):
    legacy = Store(db_path) if projects_root is None else None
    catalog = ProjectCatalog(projects_root, allowed_projects) if projects_root is not None else None
    mcp = FastMCP('Agent-X Business Wiki', host=host, port=port)

    def scoped(project_id, baseline_id):
        if catalog:
            if not project_id or not baseline_id:
                raise ValueError('project_id_and_baseline_id_required')
            return catalog.store(project_id, baseline_id)
        if project_id is not None or baseline_id is not None:
            raise ValueError('project_mode_not_enabled')
        return legacy, None

    def decorate(value, project_id, baseline_id):
        if catalog:
            value = {**value, 'project_id': project_id, 'baseline_id': baseline_id}
        return value

    def page_metadata(pages, project):
        if not project:
            return
        components = {c['id'] for c in project['config']['components']}
        for page in pages:
            if set(page.get('metadata', {}).get('component_ids', [])) - components:
                raise ValueError('unknown_page_component')

    @mcp.tool()
    async def knowledge_search(query: str, subsystems: list[str] | None = None,
                               features: list[str] | None = None, kind: str = 'all', limit: int = 8,
                               project_id: str | None = None, baseline_id: str | None = None,
                               component_ids: list[str] | None = None) -> dict:
        """Search original documents/wiki. Project mode requires explicit project and baseline; no global fallback."""
        store, _ = scoped(project_id, baseline_id)
        result = await asyncio.to_thread(store.search, query, subsystems, features, kind, limit, component_ids)
        result['results'] = [decorate(hit, project_id, baseline_id) for hit in result['results']]
        return decorate(result, project_id, baseline_id)

    @mcp.tool()
    async def knowledge_read(kind: str, item_id: str, project_id: str | None = None,
                             baseline_id: str | None = None, revision: str | None = None) -> dict:
        """Read evidence in one scope; an explicit revision reads the preserved snapshot, never latest silently."""
        store, _ = scoped(project_id, baseline_id)
        return decorate(await asyncio.to_thread(store.read, kind, item_id, revision), project_id, baseline_id)

    @mcp.tool()
    async def wiki_lint(project_id: str | None = None, baseline_id: str | None = None) -> dict:
        """Find stale references and broken wiki links inside one project/baseline."""
        store, _ = scoped(project_id, baseline_id)
        return decorate(await asyncio.to_thread(store.lint), project_id, baseline_id)

    if catalog:
        @mcp.tool()
        async def project_get(project_id: str, baseline_id: str) -> dict:
            """Resume an onboarding draft or read confirmed config/hash. This is not code build status."""
            return await asyncio.to_thread(catalog.get, project_id, baseline_id)

        @mcp.tool()
        async def project_route(project_id: str, baseline_id: str, query: str) -> dict:
            """Resolve project-specific aliases to components, features, repositories and confirmed relations."""
            return await asyncio.to_thread(catalog.route, project_id, baseline_id, query)

        if writable:
            @mcp.tool()
            async def project_init(project_id: str, baseline_id: str, title: str) -> dict:
                """Create/resume a durable initialization draft and return unanswered onboarding questions."""
                return await asyncio.to_thread(catalog.init, project_id, baseline_id, title)

            @mcp.tool()
            async def project_propose(project_id: str, baseline_id: str, patch: dict, expected_revision: int) -> dict:
                """Update a draft with structured fields; arrays replace explicitly. Confirmed baselines are frozen."""
                return await asyncio.to_thread(catalog.propose, project_id, baseline_id, patch, expected_revision)

            @mcp.tool()
            async def project_apply(project_id: str, baseline_id: str, expected_revision: int) -> dict:
                """Confirm the reviewed exact draft revision; index submission remains a separate code_build operation."""
                return await asyncio.to_thread(catalog.apply, project_id, baseline_id, expected_revision)

    if writable:
        @mcp.tool()
        async def source_ingest(source_id: str, title: str, content: str, metadata: dict, analyze: bool = False,
                                project_id: str | None = None, baseline_id: str | None = None) -> dict:
            """Store parsed source and preserve its revision. In project mode use a declared document binding."""
            store, project = scoped(project_id, baseline_id)
            if project:
                binding = next((d for d in project['config']['documents'] if d['source_id'] == source_id), None)
                if binding is None:
                    raise ValueError('document_not_registered')
                metadata = {**metadata, 'component_ids': binding.get('component_ids', []),
                            'version_status': binding['version_status']}
            result = await asyncio.to_thread(store.ingest, source_id, title, content, metadata)
            if analyze:
                try:
                    result['analysis'] = await compile_source(store, source_id)
                except Exception as exc:
                    result['analysis'] = {'status': 'failed', 'error_type': type(exc).__name__, 'retry_tool': 'wiki_compile'}
            return decorate(result, project_id, baseline_id)

        @mcp.tool()
        async def wiki_compile(source_id: str, project_id: str | None = None, baseline_id: str | None = None) -> dict:
            """Compile only within this scope and return a proposal, never auto-apply."""
            store, _ = scoped(project_id, baseline_id)
            return decorate(await compile_source(store, source_id), project_id, baseline_id)

        @mcp.tool()
        async def wiki_propose_update(pages: list[dict], project_id: str | None = None,
                                      baseline_id: str | None = None) -> dict:
            """Stage evidence-grounded scoped pages with citation and concurrency checks at apply."""
            store, project = scoped(project_id, baseline_id)
            page_metadata(pages, project)
            return decorate(await asyncio.to_thread(store.propose, pages), project_id, baseline_id)

        @mcp.tool()
        async def wiki_apply_update(proposal_id: str, project_id: str | None = None,
                                    baseline_id: str | None = None) -> dict:
            """Apply a proposal in its project/baseline; IDs from other scopes cannot resolve."""
            store, _ = scoped(project_id, baseline_id)
            return decorate(await asyncio.to_thread(store.apply, proposal_id), project_id, baseline_id)
    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.getenv('WIKI_DB_PATH', './data/business-wiki.sqlite3'))
    parser.add_argument('--projects-root', help='Enable strict project/baseline mode with isolated document databases')
    parser.add_argument('--project', action='append', default=[], help='Allowed project; repeat to allow several')
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--writable', action='store_true')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('this version binds loopback only; remote deployment requires an authenticated gateway')
    if args.project and not args.projects_root:
        parser.error('--project requires --projects-root')
    create_server(Path(args.db), args.writable, args.host, args.port, args.projects_root, args.project).run(transport=args.transport)


if __name__ == '__main__':
    main()
