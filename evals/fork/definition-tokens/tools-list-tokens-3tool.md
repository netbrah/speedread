# tools/list tokens — 3-tool surface

Headline: **1244 o200k_base tokens** (cl100k_base **1221**).

Sliced from the live 4-tool result (1578 o200k) by dropping every tool not in map, search, read. Delta -334 o200k. The kept objects are the server's bytes. Instructions are unchanged and still mention trace; they are not in this number.

Tools in this payload, server order: map, read, search.

| Tool | description | inputSchema | annotations | other | sum |
|---|---:|---:|---:|---:|---:|
| map | 57 | 165 | 29 | 10 | 261 |
| read | 244 | 165 | 29 | 9 | 447 |
| search | 94 | 383 | 29 | 9 | 515 |

Boundary 17, result.other 4, sums to the result: True.
