// One operation per process. No watchers, automatic sync or agent installation.
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
console.log = (...args) => console.error(...args);
const { CodeGraph, setLogger, silentLogger } = require(
  process.env.CODEGRAPH_MODULE || '@colbymchenry/codegraph'
);
setLogger(silentLogger);
let input = '';
for await (const chunk of process.stdin) input += chunk;
const req = JSON.parse(input);
let graph;
try {
  if (req.action === 'build') {
    graph = await CodeGraph.init(req.path);
    const result = await graph.indexAll();
    if (!result.success) throw new Error(JSON.stringify(result));
    process.stdout.write(JSON.stringify({result, stats: graph.getStats()}));
  } else {
    graph = await CodeGraph.open(req.path, {readOnly: true, sync: false});
    if (req.action === 'search') {
      const hits = graph.searchNodes(req.query, {
        limit: req.limit, includePatterns: req.patterns?.length ? req.patterns : undefined
      });
      const results = [];
      for (const hit of hits) {
        results.push({node: hit.node, score: hit.score,
          excerpt: ((await graph.getCode(hit.node.id)) || '').slice(0, 6000)});
      }
      process.stdout.write(JSON.stringify({results}));
    } else if (req.action === 'relations') {
      if (!graph.getNode(req.node_id)) throw new Error('node_not_found');
      const edges = req.direction === 'callers'
        ? graph.getCallers(req.node_id, req.depth)
        : graph.getCallees(req.node_id, req.depth);
      process.stdout.write(JSON.stringify({results: edges.slice(0, req.limit),
        truncated: edges.length > req.limit}));
    } else throw new Error('unknown_action');
  }
} catch (error) {
  console.error(error.stack || String(error));
  process.exitCode = 1;
} finally {
  graph?.close();
}
