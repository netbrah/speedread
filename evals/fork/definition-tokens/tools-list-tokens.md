# tools/list tokens — stable

Headline: **1578 o200k_base tokens**, median of 5 launches, range 0.

What that is: exact JSON of the tools/list result object, sliced from the server's response line. tiktoken 0.14.0, `Encoding.encode(text, disallowed_special=())`. cl100k_base on the same bytes: **1551**.

In the result: tool name, description, inputSchema, annotations, and any other field the server put on each tool; the tools array and the result object's own punctuation.

Not in the result: the jsonrpc envelope and id (reported as wire_line_tokens, which does include them); initialize instructions (reported separately; they are on initialize, not tools/list); client wrapping that turns definitions into a model prompt; skills/speedread/SKILL.md.

Tools observed (4): map, read, search, trace.

Attributed tokens sit on the bytes of the result object, so a token that crosses a field boundary is `boundary` and the columns plus `result.other` plus `boundary` equal the headline. `isolated` is the field encoded alone and does not add up to the headline.

| Tool | description | inputSchema | annotations | other | attributed sum | isolated description | isolated inputSchema |
|---|---:|---:|---:|---:|---:|---:|---:|
| map | 57 | 165 | 29 | 10 | 261 | 59 | 166 |
| read | 244 | 165 | 29 | 9 | 447 | 246 | 167 |
| search | 94 | 383 | 29 | 9 | 515 | 96 | 385 |
| trace | 139 | 151 | 29 | 9 | 328 | 141 | 153 |

result.other 4, boundary 23, wire line (jsonrpc envelope included) 1592.
Attribution sums to the result: True.

Instructions, rooted server, o200k text: 145. Rootless server (one launch): 185. Neither number is inside tools/list.

JSON Schema dialect URL occurs 4 times inside inputSchema; the URL alone is 13 o200k tokens. That count is inside the inputSchema column, not on top of it.

Property descriptions that are byte-identical on more than one tool:

- 4 copies, 17 o200k tokens each, at map.base_dir, read.base_dir, search.base_dir, trace.base_dir
- 2 copies, 9 o200k tokens each, at read.budget, search.budget

Server version 0.2.1-netbrah.1. Flags `--deadline-ms 10000 --cache-mb 256`. Binary `/agent/repos/speedread/target/fast/speedread`.

Limiter: tokenizer is deterministic; the only noise is a payload that changes between launches.
