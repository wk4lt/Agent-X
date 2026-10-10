"""Real stdio initialize, discovery, ingest, proposal, apply and retrieval."""
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_end_to_end(tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable,args=['-m','business_wiki.server','--db',str(tmp_path/'wiki.db'),'--writable'],env=dict(os.environ))
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert len(tools) == 7
                result = await session.call_tool('source_ingest',{'source_id':'demo','title':'WH 初始化','content':'初始化要求 VUR 就绪','metadata':{'subsystems':['WH']}})
                assert not result.isError
                rev = json.loads(result.content[0].text)['revision']
                proposal = await session.call_tool('wiki_propose_update',{'pages':[dict(id='wh-init',title='WH 初始化',content='VUR 就绪 [demo]',metadata={'subsystems':['WH']},citations=[dict(source_id='demo',revision=rev,quote='VUR 就绪')],links=[],expected_revision=0)]})
                applied = await session.call_tool('wiki_apply_update',{'proposal_id':json.loads(proposal.content[0].text)['proposal_id']})
                assert not applied.isError
                hits = await session.call_tool('knowledge_search',{'query':'初始化','subsystems':['WH'],'kind':'wiki'})
                assert json.loads(hits.content[0].text)['results'][0]['id'] == 'wh-init'
    asyncio.run(run())


def test_readonly_tools(tmp_path):
    from business_wiki.server import create_server
    async def run():
        tools = await create_server(tmp_path/'read.db').list_tools()
        assert {t.name for t in tools} == {'knowledge_search','knowledge_read','wiki_lint'}
    asyncio.run(run())
