# Chromium query baseline

Runs per query: 5. Flags: `--deadline-ms 10000 --cache-mb 256`. Tokenizer: tiktoken 0.14.0.

Load before `0.13 0.33 0.21 1/244 7188`, after `3.88 1.74 0.76 1/303 8671`.

| Query | first ms | median ms | min–max ms | median RSS MiB | RSS min–max | median o200k | o200k min–max | errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `search base::BindOnce` | 237 | 115 | 96–237 | 174 | 174–174 | 5203 | 5178–5813 | 0 |
| `search -w ScopedObservation` | 2671 | 1141 | 1129–2671 | 174 | 174–174 | 5596 | 5596–5596 | 0 |
| `search DCHECK_CURRENTLY_ON --output files` | 1027 | 1022 | 1003–1028 | 174 | 174–174 | 6095 | 6095–6095 | 0 |
| `search zzz_not_there_zzz` | 1059 | 1057 | 1041–1064 | 174 | 174–174 | 71 | 71–71 | 0 |
| `search NavigationThrottle content/browser` | 96 | 96 | 94–98 | 174 | 174–174 | 4687 | 4687–4687 | 0 |
| `read #DidCommitNavigation` | 1041 | 996 | 990–1041 | 174 | 174–174 | 4562 | 4562–4562 | 0 |
| `read content/browser/renderer_host/render_frame_host_impl.cc#...` | 85 | 83 | 81–86 | 174 | 174–174 | 2312 | 2312–2312 | 0 |
| `trace DidCommitNavigation` | 1575 | 1508 | 1470–1575 | 174 | 174–174 | 731 | 731–731 | 0 |
| `trace PostTask` | 2028 | 2067 | 2000–2111 | 175 | 174–178 | 2983 | 2983–2983 | 0 |
| `trace Run --depth 2` | 8374 | 8374 | 7851–8645 | 512 | 504–713 | 2795 | 2783–2928 | 0 |
| `trace content/browser/renderer_host/render_frame_host_impl.cc...` | 3125 | 3120 | 3084–3137 | 200 | 191–208 | 2522 | 2522–2522 | 0 |
| `map` | 744 | 750 | 737–768 | 175 | 174–179 | 2814 | 2814–2814 | 0 |
| `map content/browser` | 22 | 20 | 19–22 | 174 | 174–174 | 2452 | 2452–2452 | 0 |
| `search --ast base::BindOnce($$$A) --lang cpp content/browser/...` | 506 | 506 | 476–521 | 174 | 174–174 | 5579 | 5579–5579 | 0 |
| `search --ast base::BindOnce($$$A) --lang cpp` | 3495 | 2936 | 2812–3676 | 174 | 174–174 | 5432 | 5432–5432 | 0 |
| `search --ast std::make_unique<$T>() --lang cpp content` | 2434 | 2447 | 2395–2476 | 174 | 174–174 | 5468 | 5468–5468 | 0 |
| `search --ast zzz_not_there($A) --lang cpp` | 1038 | 1048 | 1038–1072 | 174 | 174–174 | 34 | 34–34 | 0 |

Errors: 0. Limiter: page cache, deadline, and load. A definition-token edit cannot move these times; a later latency gap larger than the min–max here is an observation error.

Profile run (not in the table):

```
{
  "ran": true,
  "in_timed_table": false,
  "why": "strace slows the work; this run is the limiter check, not a result",
  "query": [
    "search",
    "zzz_profile_not_in_the_timed_set_zzz"
  ],
  "wall_ms_under_strace": 1127.5,
  "exit_code": 0,
  "strace": "/agent/repos/speedread/evals/fork/definition-tokens/chromium/profile/strace.txt",
  "stderr_tail": "speedread-stats walks=1 entries=471431 stopped=0 cancelled=0\n"
}
```
