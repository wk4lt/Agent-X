"""MCP entry point. Read-only by default; writes require explicit server mode."""
import argparse
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .store import Store
from .compiler import compile_source


def create_server(db_path, writable=False, host='127.0.0.1', port=8765):
    store = Store(db_path)
    mcp = FastMCP('Agent-X Business Wiki', host=host, port=port)

    @mcp.tool()
    def knowledge_search(query: str, subsystems: list[str] | None = None, features: list[str] | None = None, kind: str = 'all', limit: int = 8) -> dict:
        """Search raw sources/wiki using BM25. Scope filters are exact optional tags; broaden if empty."""
        return store.search(query,subsystems,features,kind,limit)

    @mcp.tool()
    def knowledge_read(kind: str, item_id: str) -> dict:
        """Read source or wiki including source revision/citations and wiki staleness."""
        return store.read(kind,item_id)

    @mcp.tool()
    def wiki_lint() -> dict:
        """Find stale source references and broken wiki links. Does not judge semantic contradictions."""
        return store.lint()

    if writable:
        @mcp.tool()
        async def source_ingest(source_id: str, title: str, content: str, metadata: dict, analyze: bool = False) -> dict:
            """Upsert source snapshot/index. Optionally run configured LLM and return a pending wiki proposal."""
            result = store.ingest(source_id,title,content,metadata)
            if analyze:
                try:
                    result['analysis'] = await compile_source(store,source_id)
                except Exception as exc:
                    # Source is durable even if remote compilation fails; safe retry via wiki_compile.
                    result['analysis'] = {'status':'failed','error_type':type(exc).__name__, 'retry_tool':'wiki_compile'}
            return result

        @mcp.tool()
        async def wiki_compile(source_id: str) -> dict:
            """Analyze a stored source with existing wiki and return a proposal; never auto-apply."""
            return await compile_source(store,source_id)

        @mcp.tool()
        def wiki_propose_update(pages: list[dict]) -> dict:
            """Stage source-grounded wiki pages; use knowledge_read for current revisions first."""
            return store.propose(pages)

        @mcp.tool()
        def wiki_apply_update(proposal_id: str) -> dict:
            """Atomically apply proposal after source quote/revision and wiki revision validation."""
            return store.apply(proposal_id)
    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.getenv('WIKI_DB_PATH', './data/business-wiki.sqlite3'))
    parser.add_argument('--transport', choices=['stdio','streamable-http'], default='stdio')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--writable',action='store_true')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1','localhost','::1'):
        parser.error('this version binds loopback only; remote deployment requires an authenticated gateway')
    create_server(Path(args.db),args.writable,args.host,args.port).run(transport=args.transport)


if __name__ == '__main__':
    main()
