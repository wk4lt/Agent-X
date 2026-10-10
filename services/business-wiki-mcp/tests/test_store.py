import asyncio
from types import SimpleNamespace
import json
import pytest
from business_wiki.store import Store
from business_wiki.compiler import compile_source


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/'wiki.db')


def source(store, content='WHSSD 初始化要求 VUR 就绪。'):
    return store.ingest('wh-design','WHSSD 初始化',content,{'subsystems':['WH'],'features':['初始化']})


def page(revision, **overrides):
    value = dict(id='wh-init',title='WHSSD 初始化约束',content='设计要求 VUR 就绪 [wh-design]。',metadata={'subsystems':['WH'],'features':['初始化']},citations=[{'source_id':'wh-design','revision':revision,'quote':'VUR 就绪'}],links=[],expected_revision=0)
    value.update(overrides)
    return value


def test_ingest_idempotent_and_scoped_chinese_search(store):
    source(store)
    assert not source(store)['changed']
    store.ingest('rh','初始化','RH 初始化要求。',{'subsystems':['RH'],'features':['初始化']})
    hits = store.search('初始化',['WH'],['初始化'],limit=1)['results']
    assert hits[0]['id'] == 'wh-design'
    assert hits[0]['excerpt'].startswith('WHSSD')
    assert not store.search('初始化',['IS'])['results']


def test_atomic_conflict_idempotency_staleness(store):
    rev = source(store)['revision']
    proposal = store.propose([page(rev)])
    store.apply(proposal['proposal_id'])
    store.apply(proposal['proposal_id'])
    assert store.read('wiki','wh-init')['revision'] == 1
    bad = store.propose([page(rev,id='new-page'),page(rev)])
    with pytest.raises(ValueError,match='conflict'):
        store.apply(bad['proposal_id'])
    with pytest.raises(ValueError,match='not found'):
        store.read('wiki','new-page')
    source(store,'WHSSD 初始化要求 VUR 就绪，并检查联锁。')
    assert store.read('wiki','wh-init')['stale']
    assert store.lint()['stale_pages'] == ['wh-init']


def test_invalid_evidence_and_source_change_rejected(store):
    rev = source(store)['revision']
    p = page(rev)
    p['citations'][0]['quote'] = '不存在的原文'
    proposal = store.propose([p])
    with pytest.raises(ValueError,match='quotation'):
        store.apply(proposal['proposal_id'])
    proposal = store.propose([page(rev)])
    source(store,'新版本：VUR 就绪。')
    with pytest.raises(ValueError,match='revision'):
        store.apply(proposal['proposal_id'])


def test_compile_merges_existing_context_without_autoapply(store):
    rev = source(store)['revision']
    store.apply(store.propose([page(rev)])['proposal_id'])
    async def create(**kw):
        inputs = json.loads(kw['messages'][1]['content'])
        assert inputs['existing_pages'][0]['id'] == 'wh-init'
        payload = json.dumps({'pages':[page(rev,expected_revision=1)]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=payload))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = asyncio.run(compile_source(store,'wh-design',client,'fake-model'))
    assert result['status'] == 'pending'
    assert store.read('wiki','wh-init')['revision'] == 1
